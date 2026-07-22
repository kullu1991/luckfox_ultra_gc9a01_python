"""
luckfox_benchmark.py

Measures the real, on-hardware frame rate of the GC9A01 display on a Luckfox
Pico Ultra W, for a few representative workloads:

    1. Full-screen fill()          - raw pixel throughput / worst case
    2. 64x64 sprite blit           - toaster-style sprite draw
    3. Needle frame (erase + blit) - the speedometer's per-frame cost
    4. Pixel throughput (MB/s)     - derived from the full-frame time

Results are printed to the console. Run it as:

    python3 examples/luckfox_benchmark.py

Wiring used below (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 40000000

COLORS = [gc9a01.RED, gc9a01.GREEN, gc9a01.BLUE, gc9a01.YELLOW, gc9a01.WHITE]


def bench(name, iterations, func):
    """Time `func` over `iterations` and report fps + per-frame time."""
    start = time.monotonic()
    for i in range(iterations):
        func(i)
    elapsed = time.monotonic() - start
    fps = iterations / elapsed if elapsed > 0 else float("inf")
    ms = (elapsed / iterations) * 1000.0
    print("  {:<28} {:6.1f} fps   ({:6.2f} ms/frame)".format(name, fps, ms))
    return elapsed


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

        # Report the SPI clock the kernel actually granted (may be < requested,
        # because it must be an integer divisor of the SPI controller clock).
        try:
            actual_hz = spi.max_speed_hz
        except Exception:
            actual_hz = SPI_SPEED_HZ
        print("\nSPI clock requested: {:,} Hz".format(SPI_SPEED_HZ))
        print("SPI clock in use:    {:,} Hz\n".format(actual_hz))

        # A 64x64 sprite buffer (like a toaster frame) and a needle-sized one.
        sprite64 = bytes([0xF8, 0x00]) * (64 * 64)          # solid red
        needle_w, needle_h = 60, 60
        needle_buf = bytes([0xFF, 0xFF]) * (needle_w * needle_h)

        print("Benchmark results (higher fps = faster):")

        # 1. Full-screen fill: the heaviest common operation.
        full = bench(
            "full-screen fill()", 30,
            lambda i: tft.fill(COLORS[i % len(COLORS)]))

        # 2. 64x64 opaque sprite blit at a moving-ish position.
        tft.fill(gc9a01.BLACK)
        bench(
            "64x64 sprite blit", 200,
            lambda i: tft.blit_buffer(sprite64, 88, 40 + (i % 100), 64, 64))

        # 3. Speedometer needle frame: erase a box + blit a needle-sized box.
        tft.fill(gc9a01.BLACK)

        def needle_frame(i):
            y = 60 + (i % 40)
            tft.fill_rect(90, y, needle_w, needle_h, gc9a01.BLACK)
            tft.blit_buffer(needle_buf, 90, y, needle_w, needle_h)

        bench("needle frame (erase+blit)", 200, needle_frame)

        # 4. Derived raw pixel throughput from the full-frame time.
        frame_bytes = tft.width * tft.height * 2
        per_frame_s = full / 30.0
        mbps = (frame_bytes / per_frame_s) / 1_000_000.0
        print("\n  effective pixel throughput: {:.2f} MB/s".format(mbps))
        print("  (theoretical max at {:,} Hz: {:.2f} MB/s)\n".format(
            actual_hz, actual_hz / 8.0 / 1_000_000.0))

    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
