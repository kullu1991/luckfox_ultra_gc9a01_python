"""
luckfox_toasters.py

An example using bitmap to draw sprites on the display.

Spritesheet from CircuitPython_Flying_Toasters
https://learn.adafruit.com/circuitpython-sprite-animation-pendant-mario-clouds-flying-toasters

Ported from toasters.py to run on a Luckfox Pico Ultra W (RV1106) under
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib"))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01
import t1, t2, t3, t4, t5

TOASTERS = [t1, t2, t3, t4]
TOAST = [t5]

DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 40000000


class toast():
    '''
    toast class to keep track of a sprites locaton and step
    '''
    def __init__(self, sprites, x, y):
        self.sprites = sprites
        self.steps = len(sprites)
        self.x = x
        self.y = y
        self.step = random.randint(0, self.steps-1)
        self.speed = random.randint(2, 5)

    def move(self):
        if self.x <= 0:
            self.speed = random.randint(2, 5)
            self.x = 240 - 64

        self.step += 1
        self.step %= self.steps
        self.x -= self.speed


def main():
    """
    Initialize the display and draw flying toasters and toast
    """
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
        # create toast spites in random positions
        sprites = [
            toast(TOASTERS, 240-64, 0),
            toast(TOAST, 240-64*2, 80),
            toast(TOASTERS, 240-64*4, 160)
        ]

        # move and draw sprites
        while True:
            for man in sprites:
                bitmap = man.sprites[man.step]
                tft.fill_rect(
                    man.x+bitmap.WIDTH-man.speed,
                    man.y,
                    man.speed,
                    bitmap.HEIGHT,
                    gc9a01.BLACK)

                man.move()

                if man.x > 0:
                    tft.bitmap(bitmap, man.x, man.y)
                else:
                    tft.fill_rect(
                        0,
                        man.y,
                        bitmap.WIDTH,
                        bitmap.HEIGHT,
                        gc9a01.BLACK)
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
