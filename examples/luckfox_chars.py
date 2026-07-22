"""
luckfox_chars.py

Pages through all characters of four fonts on the display.

Ported from chars.py to run on a Luckfox Pico Ultra W (RV1106) under
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
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

# Choose a few fonts

# from fonts.romfonts import vga1_8x8 as font
from fonts.romfonts import vga2_8x8 as font1
# from fonts.romfonts import vga1_8x16 as font
from fonts.romfonts import vga2_8x16 as font2
# from fonts.romfonts import vga1_16x16 as font
# from fonts.romfonts import vga1_bold_16x16 as font
# from fonts.romfonts import vga2_16x16 as font
from fonts.romfonts import vga2_bold_16x16 as font3
# from fonts.romfonts import vga1_16x32 as font
# from fonts.romfonts import vga1_bold_16x32 as font
# from fonts.romfonts import vga2_16x32 as font
from fonts.romfonts import vga2_bold_16x32 as font4

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

        while True:
            for font in (font1, font2, font3, font4):
                tft.fill(gc9a01.BLUE)
                line = 0
                col = 0

                for char in range(font.FIRST, font.LAST):
                    tft.text(font, chr(char), col, line, gc9a01.WHITE, gc9a01.BLUE)
                    col += font.WIDTH
                    if col > tft.width - font.WIDTH:
                        col = 0
                        line += font.HEIGHT

                        if line > tft.height-font.HEIGHT:
                            time.sleep(3)
                            tft.fill(gc9a01.BLUE)
                            line = 0
                            col = 0

                time.sleep(3)
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
