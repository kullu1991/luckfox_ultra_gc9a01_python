"""
luckfox_lines.py

Draws lines and rectangles in random colors at random locations on the
display.

Ported from lines.py to run on a Luckfox Pico Ultra W (RV1106) under
Linux/CPython, using `spidev` for SPI and `periphery` for GPIO instead of
MicroPython's `machine` module.

Wiring used below (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import os
import random
import sys

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


def main():
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED_HZ
    spi.mode = 0

    dc = GPIO(DC_PIN, "out")
    reset = GPIO(RESET_PIN, "out")
    backlight = GPIO(BACKLIGHT_PIN, "out")

    try:
        tft = gc9a01.GC9A01(
            spi,
            dc=dc,
            reset=reset,
            backlight=backlight,
            rotation=0)

        tft.fill(gc9a01.BLACK)

        while True:
            tft.line(
                random.randint(0, tft.width),
                random.randint(0, tft.height),
                random.randint(0, tft.width),
                random.randint(0, tft.height),
                gc9a01.color565(
                    random.getrandbits(8),
                    random.getrandbits(8),
                    random.getrandbits(8)
                    )
                )

            width = random.randint(0, tft.width // 2)
            height = random.randint(0, tft.height // 2)
            col = random.randint(0, tft.width - width)
            row = random.randint(0, tft.height - height)
            tft.fill_rect(
                col,
                row,
                width,
                height,
                gc9a01.color565(
                    random.getrandbits(8),
                    random.getrandbits(8),
                    random.getrandbits(8)
                )
            )
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
