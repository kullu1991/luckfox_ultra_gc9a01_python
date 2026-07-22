"""
luckfox_chango.py

Test for font2bitmap converter for the GC9A01 display.
See the font2bitmap program in the utils directory.

Ported from chango.py to run on a Luckfox Pico Ultra W (RV1106) under
Linux/CPython, using `spidev` for SPI and `periphery` for GPIO instead of
MicroPython's `machine` module.

Wiring used below (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01py

from fonts.truetype import chango_16 as font_16
from fonts.truetype import chango_32 as font_32
from fonts.truetype import chango_64 as font_64

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
        # enable display and clear screen
        tft = gc9a01py.GC9A01(
            spi,
            dc=dc,
            reset=reset,
            backlight=backlight,
            rotation=0)

        tft.fill(gc9a01py.BLACK)

        row = 0
        tft.write(font_16, "abcdefghijklmnopqrstuvwxyz", 0, row)
        row += font_16.HEIGHT

        tft.write(font_32, "abcdefghijklm", 0, row)
        row += font_32.HEIGHT

        tft.write(font_32, "nopqrstuvwxy", 0, row)
        row += font_32.HEIGHT

        tft.write(font_64, "abcdef", 0, row)
        row += font_64.HEIGHT

        tft.write(font_64, "ghijkl", 0, row)
        row += font_64.HEIGHT
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
