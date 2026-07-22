"""
luckfox_scroll.py

Smoothly(ish) scrolls all font characters up the screen on the display.
Only works with fonts with heights that are even multiples of the screen
height, (i.e. 8 or 16 pixels high)

Ported from scroll.py to run on a Luckfox Pico Ultra W (RV1106) under
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

# choose a font

# from fonts.romfonts import vga1_8x8 as font
# from fonts.romfonts import vga2_8x8 as font
# from fonts.romfonts import vga1_8x16 as font
# from fonts.romfonts import vga2_8x16 as font
# from fonts.romfonts import vga1_16x16 as font
# from fonts.romfonts import vga1_bold_16x16 as font
# from fonts.romfonts import vga2_16x16 as font
from fonts.romfonts import vga2_bold_16x16 as font

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

        last_line = tft.height - font.HEIGHT
        tfa = 0
        tfb = 0
        tft.vscrdef(tfa, 240, tfb)

        tft.fill(gc9a01.BLUE)
        scroll = 0
        character = 0
        while True:
            tft.fill_rect(0, scroll, tft.width, 1, gc9a01.BLUE)

            if scroll % font.HEIGHT == 0:
                tft.text(
                    font,
                    'x{:02x} = {:s}'.format(character, chr(character)),
                    64,
                    (scroll + last_line) % tft.height,
                    gc9a01.WHITE,
                    gc9a01.BLUE)

                character = character + 1 if character < 256 else 0

            tft.vscsad(scroll+tfa)
            scroll += 1

            if scroll == tft.height:
                scroll = 0

            time.sleep(0.01)
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
