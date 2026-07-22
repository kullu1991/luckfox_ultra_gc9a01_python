"""
luckfox_noto_fonts.py

Writes the names of three Noto fonts centered on the display
using the font. The fonts were converted from True Type fonts using
the font2bitmap utility.

Ported from noto_fonts.py to run on a Luckfox Pico Ultra W (RV1106) under
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

import gc9a01py_linux as gc9a01

from fonts.truetype import NotoSans_32 as noto_sans
from fonts.truetype import NotoSerif_32 as noto_serif
from fonts.truetype import NotoSansMono_32 as noto_mono

DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 40000000


def main():

    def center(font, string, row, color=gc9a01.WHITE):
        screen = tft.width                        # get screen width
        width = tft.write_width(font, string)     # get the width of the string
        if width and width < screen:              # if the string < display
            col = tft.width // 2 - width // 2     # find the column to center
        else:                                     # otherwise
            col = 0                               # left justify

        tft.write(font, string, col, row, color)  # and write the string

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

        # enable display and clear screen
        tft.fill(gc9a01.BLACK)

        # center the name of the first font, using the font
        row = 64
        center(noto_sans, "NotoSans", row, gc9a01.RED)
        row += noto_sans.HEIGHT

        # center the name of the second font, using the font
        center(noto_serif, "NotoSerif", row, gc9a01.GREEN)
        row += noto_serif.HEIGHT

        # center the name of the third font, using the font
        center(noto_mono, "NotoSansMono", row, gc9a01.BLUE)
        row += noto_mono.HEIGHT

    finally:
        # shutdown spi and gpio
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
