"""
luckfox_hello.py

Writes "Hello!" in random colors at random locations on the display.

Ported from hello.py to run on a Luckfox Pico Ultra W (RV1106) under
Linux/CPython, using `spidev` for SPI and `periphery` for GPIO instead of
MicroPython's `machine` module.

Wiring used below (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)

CS is left to the SPI controller's own hardware chip-select, so no cs pin is
configured here. If your display's CS line isn't wired to the bus's hardware
CS pin, add a periphery.GPIO for it and pass it as `cs=` below.

Before running:
    - Enable the SPI bus you're using via `luckfox-config` (or the
      corresponding device tree overlay) if it isn't already enabled.
    - Confirm the bus/device numbers with `ls /sys/bus/spi/devices/`.
    - `pip install spidev periphery` on the board if not already installed.
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

# Choose a font

# from fonts.romfonts import vga1_8x8 as font
# from fonts.romfonts import vga2_8x8 as font
# from fonts.romfonts import vga1_8x16 as font
# from fonts.romfonts import vga2_8x16 as font
# from fonts.romfonts import vga1_16x16 as font
# from fonts.romfonts import vga1_bold_16x16 as font
# from fonts.romfonts import vga2_16x16 as font
# from fonts.romfonts import vga2_bold_16x16 as font
# from fonts.romfonts import vga1_16x32 as font
# from fonts.romfonts import vga1_bold_16x32 as font
# from fonts.romfonts import vga2_16x32 as font
from fonts.romfonts import vga2_bold_16x32 as font

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
            for rotation in range(8):
                tft.rotation(rotation)
                tft.fill(0)
                col_max = tft.width - font.WIDTH*6
                row_max = tft.height - font.HEIGHT

                for _ in range(25):
                    tft.text(
                        font,
                        "Hello!",
                        random.randint(0, col_max),
                        random.randint(0, row_max),
                        gc9a01.color565(
                            random.getrandbits(8),
                            random.getrandbits(8),
                            random.getrandbits(8)),
                        gc9a01.color565(
                            random.getrandbits(8),
                            random.getrandbits(8),
                            random.getrandbits(8))
                    )
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
