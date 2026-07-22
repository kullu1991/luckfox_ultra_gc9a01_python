"""
luckfox_speedometer.py

A fully dynamic virtual speedometer for the 240x240 circular GC9A01 display.

The speed randomly accelerates and decelerates between 0 and 100 (eased so it
looks like a real gauge). A white pointer needle sweeps around a colored zone
band (green -> yellow -> red) with white tick marks, and a large digital
read-out in the center shows the current speed, colored by zone.

Runs on a Luckfox Pico Ultra W (RV1106) under Linux/CPython, using `spidev`
for SPI and `periphery` for GPIO.

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

# ----------------------------------------------------------------------------
# Gauge geometry (240x240 display, circular)
# ----------------------------------------------------------------------------
CX = 120                    # center x
CY = 120                    # center y

MIN_SPEED = 0
MAX_SPEED = 100

START_ANGLE = 225.0         # angle (deg) for MIN_SPEED (down-left)
SWEEP = 270.0               # total sweep, clockwise over the top

R_BAND_IN = 104             # colored zone band
R_BAND_OUT = 118
R_TICK_MINOR_IN = 104       # minor ticks (every 5)
R_TICK_MAJOR_IN = 98        # major ticks (every 10)
R_TICK_OUT = 118
R_RING = 40                 # bezel ring around the central read-out
R_NEEDLE_TIP = 92
R_NEEDLE_BASE = 46
NEEDLE_HALF_W = 6

GRAY = gc9a01.color565(90, 90, 90)

# Central read-out box (cleared/redrawn when the integer speed changes)
NUM_BOX_X = 96
NUM_BOX_Y = 92
NUM_BOX_W = 48
NUM_BOX_H = 34


def zone_color(speed):
    """Return the band/needle color for a given speed."""
    if speed < 60:
        return gc9a01.GREEN
    if speed < 80:
        return gc9a01.YELLOW
    return gc9a01.RED


def speed_to_angle(speed):
    """Map a speed value to a gauge angle in degrees."""
    frac = (speed - MIN_SPEED) / (MAX_SPEED - MIN_SPEED)
    return START_ANGLE - SWEEP * frac


def _polar(angle_deg, radius):
    """Convert a gauge angle + radius to integer screen coordinates."""
    rad = math.radians(angle_deg)
    x = CX + radius * math.cos(rad)
    y = CY - radius * math.sin(rad)   # screen y grows downward
    return x, y


def radial_line(tft, angle_deg, r0, r1, width, color):
    """Draw a (thick) line running radially outward at the given angle."""
    rad = math.radians(angle_deg)
    dx = math.cos(rad)
    dy = -math.sin(rad)
    px, py = -dy, dx                  # unit perpendicular
    for off in range(-(width // 2), width // 2 + 1):
        x0 = int(round(CX + dx * r0 + px * off))
        y0 = int(round(CY + dy * r0 + py * off))
        x1 = int(round(CX + dx * r1 + px * off))
        y1 = int(round(CY + dy * r1 + py * off))
        tft.line(x0, y0, x1, y1, color)


def draw_circle(tft, radius, color, step=3):
    """Draw a thin circle outline centered on the gauge."""
    for a in range(0, 360, step):
        x, y = _polar(a, radius)
        tft.pixel(int(round(x)), int(round(y)), color)


def fill_triangle(tft, x0, y0, x1, y1, x2, y2, color):
    """Draw a filled triangle using horizontal scanlines."""
    pts = sorted(((y0, x0), (y1, x1), (y2, x2)))
    (ya, xa), (yb, xb), (yc, xc) = pts

    def interp(yy0, xx0, yy1, xx1, y):
        if yy1 == yy0:
            return xx0
        return xx0 + (xx1 - xx0) * (y - yy0) / (yy1 - yy0)

    if yc == ya:                      # fully degenerate (all same row)
        xl = max(0, min(xa, xb, xc))
        xr = min(239, max(xa, xb, xc))
        if 0 <= ya < 240 and xr >= xl:
            tft.hline(xl, ya, xr - xl + 1, color)
        return

    for y in range(ya, yc + 1):
        if y < 0 or y > 239:
            continue
        x_long = interp(ya, xa, yc, xc, y)
        if y < yb:
            x_short = interp(ya, xa, yb, xb, y)
        else:
            x_short = interp(yb, xb, yc, xc, y)
        xl = int(round(min(x_long, x_short)))
        xr = int(round(max(x_long, x_short)))
        xl = max(0, xl)
        xr = min(239, xr)
        if xr >= xl:
            tft.hline(xl, y, xr - xl + 1, color)


def needle_triangle(speed):
    """Return the three (x, y) vertices of the needle for a given speed."""
    rad = math.radians(speed_to_angle(speed))
    dx = math.cos(rad)
    dy = -math.sin(rad)
    px, py = -dy, dx                  # perpendicular for the base width
    tip = (CX + dx * R_NEEDLE_TIP, CY + dy * R_NEEDLE_TIP)
    bx = CX + dx * R_NEEDLE_BASE
    by = CY + dy * R_NEEDLE_BASE
    b1 = (bx + px * NEEDLE_HALF_W, by + py * NEEDLE_HALF_W)
    b2 = (bx - px * NEEDLE_HALF_W, by - py * NEEDLE_HALF_W)
    return (
        int(round(tip[0])), int(round(tip[1])),
        int(round(b1[0])), int(round(b1[1])),
        int(round(b2[0])), int(round(b2[1])),
    )


def draw_needle(tft, tri, color):
    fill_triangle(tft, tri[0], tri[1], tri[2], tri[3], tri[4], tri[5], color)


def draw_speed_value(tft, speed):
    """Redraw the big central number, colored by zone."""
    tft.fill_rect(NUM_BOX_X, NUM_BOX_Y, NUM_BOX_W, NUM_BOX_H, gc9a01.BLACK)
    s = str(speed)
    w = len(s) * bigfont.WIDTH
    x = CX - w // 2
    tft.text(bigfont, s, x, NUM_BOX_Y + 2, zone_color(speed), gc9a01.BLACK)


def draw_static(tft):
    """Draw the parts of the gauge that never change."""
    tft.fill(gc9a01.BLACK)

    # Colored zone band (dense radial lines so there are no gaps).
    steps = 480
    for i in range(steps + 1):
        frac = i / steps
        angle = START_ANGLE - SWEEP * frac
        speed = MIN_SPEED + (MAX_SPEED - MIN_SPEED) * frac
        radial_line(tft, angle, R_BAND_IN, R_BAND_OUT, 1, zone_color(speed))

    # White tick marks.
    for value in range(MIN_SPEED, MAX_SPEED + 1, 5):
        angle = speed_to_angle(value)
        if value % 10 == 0:
            radial_line(tft, angle, R_TICK_MAJOR_IN, R_TICK_OUT, 3, gc9a01.WHITE)
        else:
            radial_line(tft, angle, R_TICK_MINOR_IN, R_TICK_OUT, 1, gc9a01.WHITE)

    # Bezel ring and units label around the central read-out.
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

        draw_static(tft)

        current = 0.0
        target = random.uniform(0, 100)
        dwell = 0
        prev_tri = None
        last_int = -1

        while True:
            # Ease the current speed toward a random target; when it arrives,
            # linger briefly then pick a new target -> random ups and downs.
            diff = target - current
            current += diff * 0.06
            if abs(diff) < 0.6:
                dwell += 1
                if dwell > random.randint(8, 30):
                    target = random.uniform(0, 100)
                    dwell = 0
            current = max(0.0, min(100.0, current))

            speed = int(round(current))

            # Redraw the needle: erase the previous one, draw the new one.
            tri = needle_triangle(current)
            if prev_tri is not None:
                draw_needle(tft, prev_tri, gc9a01.BLACK)
            draw_needle(tft, tri, gc9a01.WHITE)
            prev_tri = tri

            # Update the digital read-out only when the whole number changes.
            if speed != last_int:
                draw_speed_value(tft, speed)
                last_int = speed

            time.sleep(0.02)
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
