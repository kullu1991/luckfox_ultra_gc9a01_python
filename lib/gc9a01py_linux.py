"""
Copyright (c) 2020, 2021 Russ Hughes

This file incorporates work covered by the following copyright and
permission notice and is licensed under the same terms:

The MIT License (MIT)

Copyright (c) 2019 Ivan Belokobylskiy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.


GC9A01 Display driver, ported from the MicroPython gc9a01py module to run
under CPython 3 on Linux boards such as the Luckfox Pico Ultra (RV1106).

Instead of `machine.Pin` / `machine.SPI`, this port expects:

  - `spi`      : an opened and configured `spidev.SpiDev()` instance
  - `dc`       : a `periphery.GPIO` instance opened as "out" for the D/C pin
  - `reset`    : a `periphery.GPIO` instance opened as "out" for the reset pin
  - `backlight`: a `periphery.GPIO` instance opened as "out" for the backlight
  - `cs`       : optional `periphery.GPIO` instance for a manually toggled
                 chip-select. Leave as None to rely on the SPI controller's
                 own hardware chip-select (the normal case on Luckfox boards,
                 where CS is toggled automatically by the kernel spidev
                 driver once per `spi.writebytes2()` call).

The driver supports display rotation, mirroring, scrolling and drawing text
using 8 and 16 bit wide bitmap fonts with heights that are multiples of 8.

If you are looking for a faster driver with additional features, check out the
C version of the driver at https://github.com/russhughes/gc9a01_mpy
"""

# pylint: disable=invalid-name,import-error

import time
import struct

# commands
GC9A01_SWRESET = 0x01
GC9A01_SLPIN = 0x10
GC9A01_SLPOUT = 0x11
GC9A01_INVOFF = 0x20
GC9A01_INVON = 0x21
GC9A01_DISPOFF = 0x28
GC9A01_DISPON = 0x29
GC9A01_CASET = 0x2A
GC9A01_RASET = 0x2B
GC9A01_RAMWR = 0x2C
GC9A01_VSCRDEF = 0x33
GC9A01_COLMOD = 0x3A
GC9A01_MADCTL = 0x36
GC9A01_VSCSAD = 0x37

# Color definitions
BLACK = 0x0000
BLUE = 0x001F
RED = 0xF800
GREEN = 0x07E0
CYAN = 0x07FF
MAGENTA = 0xF81F
YELLOW = 0xFFE0
WHITE = 0xFFFF

_ENCODE_PIXEL = ">H"
_ENCODE_POS = ">HH"
_DECODE_PIXEL = ">BBB"

_BUFFER_SIZE = 256

# Number of pixels per SPI transfer when filling. Larger transfers amortize
# the per-call dc GPIO write + spidev ioctl overhead. 16384 px = 32 KiB, a
# whole 240x240 fill is ~3.5 transfers. Bounded so a huge fill_rect doesn't
# allocate an enormous temporary buffer.
_FILL_CHUNK = 16384

_BIT7 = 0x80
_BIT6 = 0x40
_BIT5 = 0x20
_BIT4 = 0x10
_BIT3 = 0x08
_BIT2 = 0x04
_BIT1 = 0x02
_BIT0 = 0x01

ROTATIONS = [
    0x48,   # 0 - PORTRAIT
    0x28,   # 1 - LANDSCAPE
    0x88,   # 2 - INVERTED_PORTRAIT
    0xe8,   # 3 - INVERTED_LANDSCAPE
    0x08,   # 4 - PORTRAIT_MIRRORED
    0x68,   # 5 - LANDSCAPE_MIRRORED
    0xc8,   # 6 - INVERTED_PORTRAIT_MIRRORED
    0xa8]   # 7 - INVERTED_LANDSCAPE_MIRRORED]


def color565(red, green=0, blue=0):
    """
    Convert red, green and blue values (0-255) into a 16-bit 565 encoded color.
    """
    try:
        red, green, blue = red  # see if the first var is a tuple/list
    except TypeError:
        pass
    return (red & 0xf8) << 8 | (green & 0xfc) << 3 | blue >> 3


def _encode_pos(x, y):
    """Encode a postion into bytes."""
    return struct.pack(_ENCODE_POS, x, y)


def _encode_pixel(color):
    """Encode a pixel color into bytes."""
    return struct.pack(_ENCODE_PIXEL, color)


class GC9A01():
    """
    GC9A01 driver class

    Args:
        spi (spidev.SpiDev): opened and configured spidev instance (Required)
        dc (periphery.GPIO): dc pin, opened as "out" (Required)
        cs (periphery.GPIO): optional manually toggled cs pin. Leave None to
            use the SPI controller's hardware chip-select.
        reset (periphery.GPIO): reset pin, opened as "out"
        backlight (periphery.GPIO): backlight pin, opened as "out"
        rotation (int): display rotation
    """

    def __init__(
            self,
            spi=None,
            dc=None,
            cs=None,
            reset=None,
            backlight=None,
            rotation=0):
        """
        Initialize display.
        """
        if spi is None:
            raise ValueError("SPI object is required.")

        if dc is None:
            raise ValueError("dc pin is required.")

        self.width = 240
        self.height = 240
        self.spi = spi
        self.reset = reset
        self.dc = dc
        self.cs = cs
        self.backlight = backlight
        self._rotation = rotation % 8

        self.init()

    def init(self):
        """
        Hard reset the panel and send the full configuration. This is what
        __init__ runs, factored out so it can be re-run at runtime to fully
        recover a display that has gone blank / lost its state.
        """
        self.hard_reset()
        time.sleep(0.1)
        self.configure()

    def configure(self, sleepout_delay=0.12):
        """
        Send the complete register setup, sleep-out, display-on and backlight
        WITHOUT pulsing the hardware reset line. Re-sending this re-establishes
        colour mode, rotation, gamma, etc., so it can recover a panel that
        silently reset to its power-on defaults (which a bare DISPON cannot).

        sleepout_delay is the pause after the Sleep Out (0x11) command. Per the
        GC9A01A datasheet (6.2.4) only 5 ms is required after Sleep Out before
        the next command; the default 0.12 s is the conservative cold-start
        value. Recovery heartbeats pass ~0.005 s so the pause is invisible to
        the frame rate. Sleep Out is a no-op with no visual glitch when the
        panel is already awake, so it is safe to include every time.

        It issues commands, so it ends any in-progress pixel write -- re-set
        the drawing window afterwards before streaming more pixels.
        """
        self._write(0xEF)
        self._write(0xEB, b'\x14')
        self._write(0xFE)
        self._write(0xEF)
        self._write(0xEB, b'\x14')
        self._write(0x84, b'\x40')
        self._write(0x85, b'\xFF')
        self._write(0x86, b'\xFF')
        self._write(0x87, b'\xFF')
        self._write(0x88, b'\x0A')
        self._write(0x89, b'\x21')
        self._write(0x8A, b'\x00')
        self._write(0x8B, b'\x80')
        self._write(0x8C, b'\x01')
        self._write(0x8D, b'\x01')
        self._write(0x8E, b'\xFF')
        self._write(0x8F, b'\xFF')
        self._write(0xB6, b'\x00\x00')
        self._write(0x3A, b'\x55')
        self._write(0x90, b'\x08\x08\x08\x08')
        self._write(0xBD, b'\x06')
        self._write(0xBC, b'\x00')
        self._write(0xFF, b'\x60\x01\x04')
        self._write(0xC3, b'\x13')
        self._write(0xC4, b'\x13')
        self._write(0xC9, b'\x22')
        self._write(0xBE, b'\x11')
        self._write(0xE1, b'\x10\x0E')
        self._write(0xDF, b'\x21\x0c\x02')
        self._write(0xF0, b'\x45\x09\x08\x08\x26\x2A')
        self._write(0xF1, b'\x43\x70\x72\x36\x37\x6F')
        self._write(0xF2, b'\x45\x09\x08\x08\x26\x2A')
        self._write(0xF3, b'\x43\x70\x72\x36\x37\x6F')
        self._write(0xED, b'\x1B\x0B')
        self._write(0xAE, b'\x77')
        self._write(0xCD, b'\x63')
        self._write(0x70, b'\x07\x07\x04\x0E\x0F\x09\x07\x08\x03')
        self._write(0xE8, b'\x34')

        self._write(
            0x62,
            b'\x18\x0D\x71\xED\x70\x70\x18\x0F\x71\xEF\x70\x70')

        self._write(
            0x63,
            b'\x18\x11\x71\xF1\x70\x70\x18\x13\x71\xF3\x70\x70')

        self._write(0x64, b'\x28\x29\xF1\x01\xF1\x00\x07')
        self._write(
            0x66,
            b'\x3C\x00\xCD\x67\x45\x45\x10\x00\x00\x00')

        self._write(
            0x67,
            b'\x00\x3C\x00\x00\x00\x01\x54\x10\x32\x98')

        self._write(0x74, b'\x10\x85\x80\x00\x00\x4E\x00')
        self._write(0x98, b'\x3e\x07')
        self._write(0x35)
        self._write(0x21)
        self._write(0x11)               # Sleep Out
        time.sleep(sleepout_delay)      # datasheet 6.2.4: >= 5 ms required
        self._write(0x29)               # Display On

        self.rotation(self._rotation)

        if self.backlight is not None:
            self.backlight.write(True)

    def _write(self, command=None, data=None):
        """SPI write to the device: commands and data."""
        if self.cs:
            self.cs.write(False)

        if command is not None:
            self.dc.write(False)
            self.spi.writebytes2(bytes([command]))
        if data is not None:
            self.dc.write(True)
            self.spi.writebytes2(data)

        if self.cs:
            self.cs.write(True)

    def hard_reset(self):
        """Hard reset display."""
        if self.reset:
            if self.cs:
                self.cs.write(False)

            self.reset.write(True)
            time.sleep(0.05)
            self.reset.write(False)
            time.sleep(0.05)
            self.reset.write(True)
            time.sleep(0.15)

            if self.cs:
                self.cs.write(True)

    def soft_reset(self):
        """Soft reset display."""
        self._write(GC9A01_SWRESET)
        time.sleep(0.15)

    def sleep_mode(self, value):
        """
        Enable or disable display sleep mode.

        Args:
            value (bool): if True enable sleep mode.
                if False disable sleep mode
        """
        if value:
            self._write(GC9A01_SLPIN)
        else:
            self._write(GC9A01_SLPOUT)

    def inversion_mode(self, value):
        """
        Enable or disable display inversion mode.

        Args:
            value (bool): if True enable inversion mode.
                if False disable inversion mode
        """
        if value:
            self._write(GC9A01_INVON)
        else:
            self._write(GC9A01_INVOFF)

    def display_on(self):
        """
        Turn the display output on (DISPON). Re-sending this periodically
        acts as a keep-awake heartbeat so the panel does not blank.

        Note: this is a command, so it ends any in-progress pixel write.
        Call _set_window() again before streaming more pixels.
        """
        self._write(GC9A01_DISPON)

    def display_off(self):
        """Turn the display output off (DISPOFF)."""
        self._write(GC9A01_DISPOFF)

    def keep_awake(self, full=False):
        """
        Keep-awake / recovery heartbeat.

        By default this re-sends the entire register configuration + Sleep Out
        + Display On with only the datasheet-minimum 5 ms Sleep Out settle
        (configure(sleepout_delay=0.005)). That recovers a panel that silently
        blanked, lost its config, or got knocked into Sleep In -- something a
        bare DISPON cannot do -- in about 10 ms total, so it does not dent the
        frame rate. (A plain display-on heartbeat is not enough for a display
        that only a restart fixes.)

        Pass full=True to also pulse the hardware reset line first (init()),
        the strongest recovery, for when even a reconfigure is not enough.

        Either way this issues commands, so re-set the drawing window
        afterwards before streaming more pixels.
        """
        if full:
            self.init()
        else:
            self.configure(sleepout_delay=0.005)

    def rotation(self, rotation):
        """
        Set display rotation.

        Args:
            rotation (int):

                - 0 - PORTRAIT
                - 1 - LANDSCAPE
                - 2 - INVERTED PORTRAIT
                - 3 - INVERTED LANDSCAPE
                - 4 - PORTRAIT MIRRORED
                - 5 - LANDSCAPE MIRRORED
                - 6 - INVERTED PORTRAIT MIRRORED
                - 7 - INVERTED LANDSCAPE MIRRORED

        """

        self._rotation = rotation % 8
        self._write(GC9A01_MADCTL, bytes([ROTATIONS[self._rotation]]))

    def _set_columns(self, start, end):
        """
        Send CASET (column address set) command to display.

        Args:
            start (int): column start address
            end (int): column end address
        """
        if start <= end <= self.width:
            self._write(GC9A01_CASET, _encode_pos(
                start, end))

    def _set_rows(self, start, end):
        """
        Send RASET (row address set) command to display.

        Args:
            start (int): row start address
            end (int): row end address
       """
        if start <= end <= self.height:
            self._write(GC9A01_RASET, _encode_pos(
                start, end))

    def _set_window(self, x0, y0, x1, y1):
        """
        Set window to column and row address.

        Args:
            x0 (int): column start address
            y0 (int): row start address
            x1 (int): column end address
            y1 (int): row end address
        """
        self._set_columns(x0, x1)
        self._set_rows(y0, y1)
        self._write(GC9A01_RAMWR)

    def vline(self, x, y, length, color):
        """
        Draw vertical line at the given location and color.

        Args:
            x (int): x coordinate
            Y (int): y coordinate
            length (int): length of line
            color (int): 565 encoded color
        """
        self.fill_rect(x, y, 1, length, color)

    def hline(self, x, y, length, color):
        """
        Draw horizontal line at the given location and color.

        Args:
            x (int): x coordinate
            Y (int): y coordinate
            length (int): length of line
            color (int): 565 encoded color
        """
        self.fill_rect(x, y, length, 1, color)

    def pixel(self, x, y, color):
        """
        Draw a pixel at the given location and color.

        Args:
            x (int): x coordinate
            Y (int): y coordinate
            color (int): 565 encoded color
        """
        self._set_window(x, y, x, y)
        self._write(None, _encode_pixel(color))

    def blit_buffer(self, buffer, x, y, width, height):
        """
        Copy buffer to display at the given location.

        Args:
            buffer (bytes): Data to copy to display
            x (int): Top left corner x coordinate
            Y (int): Top left corner y coordinate
            width (int): Width
            height (int): Height
        """
        self._set_window(x, y, x + width - 1, y + height - 1)
        self._write(None, buffer)

    def rect(self, x, y, w, h, color):
        """
        Draw a rectangle at the given location, size and color.

        Args:
            x (int): Top left corner x coordinate
            y (int): Top left corner y coordinate
            width (int): Width in pixels
            height (int): Height in pixels
            color (int): 565 encoded color
        """
        self.hline(x, y, w, color)
        self.vline(x, y, h, color)
        self.vline(x + w - 1, y, h, color)
        self.hline(x, y + h - 1, w, color)

    def fill_rect(self, x, y, width, height, color):
        """
        Draw a rectangle at the given location, size and filled with color.

        Args:
            x (int): Top left corner x coordinate
            y (int): Top left corner y coordinate
            width (int): Width in pixels
            height (int): Height in pixels
            color (int): 565 encoded color
        """
        self._set_window(x, y, x + width - 1, y + height - 1)
        total = width * height
        pixel = _encode_pixel(color)

        # Send the fill as a few large SPI transfers instead of many tiny
        # ones. On Linux each transfer costs a dc GPIO write plus a spidev
        # ioctl, so the old 256-pixel chunking spent most of its time on
        # per-chunk overhead rather than moving pixels. We set dc once and
        # write directly, chunking only to cap peak buffer allocation.
        if self.cs:
            self.cs.write(False)
        self.dc.write(True)

        chunks, rest = divmod(total, _FILL_CHUNK)
        if chunks:
            data = pixel * _FILL_CHUNK
            for _ in range(chunks):
                self.spi.writebytes2(data)
        if rest:
            self.spi.writebytes2(pixel * rest)

        if self.cs:
            self.cs.write(True)

    def fill(self, color):
        """
        Fill the entire FrameBuffer with the specified color.

        Args:
            color (int): 565 encoded color
        """
        self.fill_rect(0, 0, self.width, self.height, color)

    def line(self, x0, y0, x1, y1, color):
        """
        Draw a single pixel wide line starting at x0, y0 and ending at x1, y1.

        Args:
            x0 (int): Start point x coordinate
            y0 (int): Start point y coordinate
            x1 (int): End point x coordinate
            y1 (int): End point y coordinate
            color (int): 565 encoded color
        """
        steep = abs(y1 - y0) > abs(x1 - x0)
        if steep:
            x0, y0 = y0, x0
            x1, y1 = y1, x1
        if x0 > x1:
            x0, x1 = x1, x0
            y0, y1 = y1, y0
        dx = x1 - x0
        dy = abs(y1 - y0)
        err = dx // 2
        if y0 < y1:
            ystep = 1
        else:
            ystep = -1
        while x0 <= x1:
            if steep:
                self.pixel(y0, x0, color)
            else:
                self.pixel(x0, y0, color)
            err -= dy
            if err < 0:
                y0 += ystep
                err += dx
            x0 += 1

    def vscrdef(self, tfa, vsa, bfa):
        """
        Set Vertical Scrolling Definition.

        To scroll a 135x240 display these values should be 40, 240, 40.
        There are 40 lines above the display that are not shown followed by
        240 lines that are shown followed by 40 more lines that are not shown.
        You could write to these areas off display and scroll them into view by
        changing the TFA, VSA and BFA values.

        Args:
            tfa (int): Top Fixed Area
            vsa (int): Vertical Scrolling Area
            bfa (int): Bottom Fixed Area
        """
        self._write(GC9A01_VSCRDEF, struct.pack(">HHH", tfa, vsa, bfa))

    def vscsad(self, vssa):
        """
        Set Vertical Scroll Start Address of RAM.

        Defines which line in the Frame Memory will be written as the first
        line after the last line of the Top Fixed Area on the display

        Example:

            for line in range(40, 280, 1):
                tft.vscsad(line)
                time.sleep(0.01)

        Args:
            vssa (int): Vertical Scrolling Start Address

        """
        self._write(GC9A01_VSCSAD, struct.pack(">H", vssa))

    def _text8(self, font, text, x0, y0, color=WHITE, background=BLACK):
        """
        Internal method to write characters with width of 8 and
        heights of 8 or 16.

        Args:
            font (module): font module to use
            text (str): text to write
            x0 (int): column to start drawing at
            y0 (int): row to start drawing at
            color (int): 565 encoded color to use for characters
            background (int): 565 encoded color to use for background
        """
        for char in text:
            ch = ord(char)
            if (font.FIRST <= ch < font.LAST
                    and x0+font.WIDTH <= self.width
                    and y0+font.HEIGHT <= self.height):

                if font.HEIGHT == 8:
                    passes = 1
                    size = 8
                    each = 0
                else:
                    passes = 2
                    size = 16
                    each = 8

                for line in range(passes):
                    idx = (ch-font.FIRST)*size+(each*line)
                    #
                    # Yes, this looks bad, but it is fast
                    #
                    buffer = struct.pack(
                        '>64H',
                        color if font.FONT[idx] & _BIT7 else background,
                        color if font.FONT[idx] & _BIT6 else background,
                        color if font.FONT[idx] & _BIT5 else background,
                        color if font.FONT[idx] & _BIT4 else background,
                        color if font.FONT[idx] & _BIT3 else background,
                        color if font.FONT[idx] & _BIT2 else background,
                        color if font.FONT[idx] & _BIT1 else background,
                        color if font.FONT[idx] & _BIT0 else background,
                        color if font.FONT[idx+1] & _BIT7 else background,
                        color if font.FONT[idx+1] & _BIT6 else background,
                        color if font.FONT[idx+1] & _BIT5 else background,
                        color if font.FONT[idx+1] & _BIT4 else background,
                        color if font.FONT[idx+1] & _BIT3 else background,
                        color if font.FONT[idx+1] & _BIT2 else background,
                        color if font.FONT[idx+1] & _BIT1 else background,
                        color if font.FONT[idx+1] & _BIT0 else background,
                        color if font.FONT[idx+2] & _BIT7 else background,
                        color if font.FONT[idx+2] & _BIT6 else background,
                        color if font.FONT[idx+2] & _BIT5 else background,
                        color if font.FONT[idx+2] & _BIT4 else background,
                        color if font.FONT[idx+2] & _BIT3 else background,
                        color if font.FONT[idx+2] & _BIT2 else background,
                        color if font.FONT[idx+2] & _BIT1 else background,
                        color if font.FONT[idx+2] & _BIT0 else background,
                        color if font.FONT[idx+3] & _BIT7 else background,
                        color if font.FONT[idx+3] & _BIT6 else background,
                        color if font.FONT[idx+3] & _BIT5 else background,
                        color if font.FONT[idx+3] & _BIT4 else background,
                        color if font.FONT[idx+3] & _BIT3 else background,
                        color if font.FONT[idx+3] & _BIT2 else background,
                        color if font.FONT[idx+3] & _BIT1 else background,
                        color if font.FONT[idx+3] & _BIT0 else background,
                        color if font.FONT[idx+4] & _BIT7 else background,
                        color if font.FONT[idx+4] & _BIT6 else background,
                        color if font.FONT[idx+4] & _BIT5 else background,
                        color if font.FONT[idx+4] & _BIT4 else background,
                        color if font.FONT[idx+4] & _BIT3 else background,
                        color if font.FONT[idx+4] & _BIT2 else background,
                        color if font.FONT[idx+4] & _BIT1 else background,
                        color if font.FONT[idx+4] & _BIT0 else background,
                        color if font.FONT[idx+5] & _BIT7 else background,
                        color if font.FONT[idx+5] & _BIT6 else background,
                        color if font.FONT[idx+5] & _BIT5 else background,
                        color if font.FONT[idx+5] & _BIT4 else background,
                        color if font.FONT[idx+5] & _BIT3 else background,
                        color if font.FONT[idx+5] & _BIT2 else background,
                        color if font.FONT[idx+5] & _BIT1 else background,
                        color if font.FONT[idx+5] & _BIT0 else background,
                        color if font.FONT[idx+6] & _BIT7 else background,
                        color if font.FONT[idx+6] & _BIT6 else background,
                        color if font.FONT[idx+6] & _BIT5 else background,
                        color if font.FONT[idx+6] & _BIT4 else background,
                        color if font.FONT[idx+6] & _BIT3 else background,
                        color if font.FONT[idx+6] & _BIT2 else background,
                        color if font.FONT[idx+6] & _BIT1 else background,
                        color if font.FONT[idx+6] & _BIT0 else background,
                        color if font.FONT[idx+7] & _BIT7 else background,
                        color if font.FONT[idx+7] & _BIT6 else background,
                        color if font.FONT[idx+7] & _BIT5 else background,
                        color if font.FONT[idx+7] & _BIT4 else background,
                        color if font.FONT[idx+7] & _BIT3 else background,
                        color if font.FONT[idx+7] & _BIT2 else background,
                        color if font.FONT[idx+7] & _BIT1 else background,
                        color if font.FONT[idx+7] & _BIT0 else background
                    )
                    self.blit_buffer(buffer, x0, y0+8*line, 8, 8)

                x0 += 8

    def _text16(self, font, text, x0, y0, color=WHITE, background=BLACK):
        """
        Internal method to draw characters with width of 16 and heights of 16
        or 32.

        Args:
            font (module): font module to use
            text (str): text to write
            x0 (int): column to start drawing at
            y0 (int): row to start drawing at
            color (int): 565 encoded color to use for characters
            background (int): 565 encoded color to use for background
        """
        for char in text:
            ch = ord(char)
            if (font.FIRST <= ch < font.LAST
                    and x0+font.WIDTH <= self.width
                    and y0+font.HEIGHT <= self.height):

                if font.HEIGHT == 16:
                    passes = 2
                    size = 32
                    each = 16
                else:
                    passes = 4
                    size = 64
                    each = 16

                for line in range(passes):
                    idx = (ch-font.FIRST)*size+(each*line)
                    #
                    # And this looks even worse, but it is fast
                    #
                    buffer = struct.pack(
                        '>128H',
                        color if font.FONT[idx] & _BIT7 else background,
                        color if font.FONT[idx] & _BIT6 else background,
                        color if font.FONT[idx] & _BIT5 else background,
                        color if font.FONT[idx] & _BIT4 else background,
                        color if font.FONT[idx] & _BIT3 else background,
                        color if font.FONT[idx] & _BIT2 else background,
                        color if font.FONT[idx] & _BIT1 else background,
                        color if font.FONT[idx] & _BIT0 else background,
                        color if font.FONT[idx+1] & _BIT7 else background,
                        color if font.FONT[idx+1] & _BIT6 else background,
                        color if font.FONT[idx+1] & _BIT5 else background,
                        color if font.FONT[idx+1] & _BIT4 else background,
                        color if font.FONT[idx+1] & _BIT3 else background,
                        color if font.FONT[idx+1] & _BIT2 else background,
                        color if font.FONT[idx+1] & _BIT1 else background,
                        color if font.FONT[idx+1] & _BIT0 else background,
                        color if font.FONT[idx+2] & _BIT7 else background,
                        color if font.FONT[idx+2] & _BIT6 else background,
                        color if font.FONT[idx+2] & _BIT5 else background,
                        color if font.FONT[idx+2] & _BIT4 else background,
                        color if font.FONT[idx+2] & _BIT3 else background,
                        color if font.FONT[idx+2] & _BIT2 else background,
                        color if font.FONT[idx+2] & _BIT1 else background,
                        color if font.FONT[idx+2] & _BIT0 else background,
                        color if font.FONT[idx+3] & _BIT7 else background,
                        color if font.FONT[idx+3] & _BIT6 else background,
                        color if font.FONT[idx+3] & _BIT5 else background,
                        color if font.FONT[idx+3] & _BIT4 else background,
                        color if font.FONT[idx+3] & _BIT3 else background,
                        color if font.FONT[idx+3] & _BIT2 else background,
                        color if font.FONT[idx+3] & _BIT1 else background,
                        color if font.FONT[idx+3] & _BIT0 else background,
                        color if font.FONT[idx+4] & _BIT7 else background,
                        color if font.FONT[idx+4] & _BIT6 else background,
                        color if font.FONT[idx+4] & _BIT5 else background,
                        color if font.FONT[idx+4] & _BIT4 else background,
                        color if font.FONT[idx+4] & _BIT3 else background,
                        color if font.FONT[idx+4] & _BIT2 else background,
                        color if font.FONT[idx+4] & _BIT1 else background,
                        color if font.FONT[idx+4] & _BIT0 else background,
                        color if font.FONT[idx+5] & _BIT7 else background,
                        color if font.FONT[idx+5] & _BIT6 else background,
                        color if font.FONT[idx+5] & _BIT5 else background,
                        color if font.FONT[idx+5] & _BIT4 else background,
                        color if font.FONT[idx+5] & _BIT3 else background,
                        color if font.FONT[idx+5] & _BIT2 else background,
                        color if font.FONT[idx+5] & _BIT1 else background,
                        color if font.FONT[idx+5] & _BIT0 else background,
                        color if font.FONT[idx+6] & _BIT7 else background,
                        color if font.FONT[idx+6] & _BIT6 else background,
                        color if font.FONT[idx+6] & _BIT5 else background,
                        color if font.FONT[idx+6] & _BIT4 else background,
                        color if font.FONT[idx+6] & _BIT3 else background,
                        color if font.FONT[idx+6] & _BIT2 else background,
                        color if font.FONT[idx+6] & _BIT1 else background,
                        color if font.FONT[idx+6] & _BIT0 else background,
                        color if font.FONT[idx+7] & _BIT7 else background,
                        color if font.FONT[idx+7] & _BIT6 else background,
                        color if font.FONT[idx+7] & _BIT5 else background,
                        color if font.FONT[idx+7] & _BIT4 else background,
                        color if font.FONT[idx+7] & _BIT3 else background,
                        color if font.FONT[idx+7] & _BIT2 else background,
                        color if font.FONT[idx+7] & _BIT1 else background,
                        color if font.FONT[idx+7] & _BIT0 else background,
                        color if font.FONT[idx+8] & _BIT7 else background,
                        color if font.FONT[idx+8] & _BIT6 else background,
                        color if font.FONT[idx+8] & _BIT5 else background,
                        color if font.FONT[idx+8] & _BIT4 else background,
                        color if font.FONT[idx+8] & _BIT3 else background,
                        color if font.FONT[idx+8] & _BIT2 else background,
                        color if font.FONT[idx+8] & _BIT1 else background,
                        color if font.FONT[idx+8] & _BIT0 else background,
                        color if font.FONT[idx+9] & _BIT7 else background,
                        color if font.FONT[idx+9] & _BIT6 else background,
                        color if font.FONT[idx+9] & _BIT5 else background,
                        color if font.FONT[idx+9] & _BIT4 else background,
                        color if font.FONT[idx+9] & _BIT3 else background,
                        color if font.FONT[idx+9] & _BIT2 else background,
                        color if font.FONT[idx+9] & _BIT1 else background,
                        color if font.FONT[idx+9] & _BIT0 else background,
                        color if font.FONT[idx+10] & _BIT7 else background,
                        color if font.FONT[idx+10] & _BIT6 else background,
                        color if font.FONT[idx+10] & _BIT5 else background,
                        color if font.FONT[idx+10] & _BIT4 else background,
                        color if font.FONT[idx+10] & _BIT3 else background,
                        color if font.FONT[idx+10] & _BIT2 else background,
                        color if font.FONT[idx+10] & _BIT1 else background,
                        color if font.FONT[idx+10] & _BIT0 else background,
                        color if font.FONT[idx+11] & _BIT7 else background,
                        color if font.FONT[idx+11] & _BIT6 else background,
                        color if font.FONT[idx+11] & _BIT5 else background,
                        color if font.FONT[idx+11] & _BIT4 else background,
                        color if font.FONT[idx+11] & _BIT3 else background,
                        color if font.FONT[idx+11] & _BIT2 else background,
                        color if font.FONT[idx+11] & _BIT1 else background,
                        color if font.FONT[idx+11] & _BIT0 else background,
                        color if font.FONT[idx+12] & _BIT7 else background,
                        color if font.FONT[idx+12] & _BIT6 else background,
                        color if font.FONT[idx+12] & _BIT5 else background,
                        color if font.FONT[idx+12] & _BIT4 else background,
                        color if font.FONT[idx+12] & _BIT3 else background,
                        color if font.FONT[idx+12] & _BIT2 else background,
                        color if font.FONT[idx+12] & _BIT1 else background,
                        color if font.FONT[idx+12] & _BIT0 else background,
                        color if font.FONT[idx+13] & _BIT7 else background,
                        color if font.FONT[idx+13] & _BIT6 else background,
                        color if font.FONT[idx+13] & _BIT5 else background,
                        color if font.FONT[idx+13] & _BIT4 else background,
                        color if font.FONT[idx+13] & _BIT3 else background,
                        color if font.FONT[idx+13] & _BIT2 else background,
                        color if font.FONT[idx+13] & _BIT1 else background,
                        color if font.FONT[idx+13] & _BIT0 else background,
                        color if font.FONT[idx+14] & _BIT7 else background,
                        color if font.FONT[idx+14] & _BIT6 else background,
                        color if font.FONT[idx+14] & _BIT5 else background,
                        color if font.FONT[idx+14] & _BIT4 else background,
                        color if font.FONT[idx+14] & _BIT3 else background,
                        color if font.FONT[idx+14] & _BIT2 else background,
                        color if font.FONT[idx+14] & _BIT1 else background,
                        color if font.FONT[idx+14] & _BIT0 else background,
                        color if font.FONT[idx+15] & _BIT7 else background,
                        color if font.FONT[idx+15] & _BIT6 else background,
                        color if font.FONT[idx+15] & _BIT5 else background,
                        color if font.FONT[idx+15] & _BIT4 else background,
                        color if font.FONT[idx+15] & _BIT3 else background,
                        color if font.FONT[idx+15] & _BIT2 else background,
                        color if font.FONT[idx+15] & _BIT1 else background,
                        color if font.FONT[idx+15] & _BIT0 else background
                    )
                    self.blit_buffer(buffer, x0, y0+8*line, 16, 8)
            x0 += font.WIDTH

    def text(self, font, text, x0, y0, color=WHITE, background=BLACK):
        """
        Draw text on display in specified font and colors. 8 and 16 bit wide
        fonts are supported.

        Args:
            font (module): font module to use.
            text (str): text to write
            x0 (int): column to start drawing at
            y0 (int): row to start drawing at
            color (int): 565 encoded color to use for characters
            background (int): 565 encoded color to use for background
        """
        if font.WIDTH == 8:
            self._text8(font, text, x0, y0, color, background)
        else:
            self._text16(font, text, x0, y0, color, background)

    def bitmap(self, bitmap, x, y, index=0):
        """
        Draw a bitmap on display at the specified column and row

        Args:
            bitmap (bitmap_module): The module containing the bitmap to draw
            x (int): column to start drawing at
            y (int): row to start drawing at
            index (int): Optional index of bitmap to draw from multiple bitmap
                module

        """
        bitmap_size = bitmap.HEIGHT * bitmap.WIDTH
        buffer_len = bitmap_size * 2
        buffer = bytearray(buffer_len)
        bs_bit = bitmap.BPP * bitmap_size * index if index > 0 else 0

        for i in range(0, buffer_len, 2):
            color_index = 0
            for bit in range(bitmap.BPP):
                color_index <<= 1
                color_index |= (bitmap.BITMAP[bs_bit // 8]
                                & 1 << (7 - (bs_bit % 8))) > 0
                bs_bit += 1

            color = bitmap.PALETTE[color_index]
            buffer[i] = color & 0xff00 >> 8
            buffer[i + 1] = color_index & 0xff

        self.blit_buffer(buffer, x, y, bitmap.WIDTH, bitmap.HEIGHT)

    def write(self, font, string, x, y, fg=WHITE, bg=BLACK):
        """
        Write a string using a converted true-type font on the display starting
        at the specified column and row

        Args:
            font (font): The module containing the converted true-type font
            s (string): The string to write
            x (int): column to start writing
            y (int): row to start writing
            fg (int): foreground color, optional, defaults to WHITE
            bg (int): background color, optional, defaults to BLACK
        """
        buffer_len = font.HEIGHT * font.MAX_WIDTH * 2
        buffer = bytearray(buffer_len)
        fg_hi = (fg & 0xff00) >> 8
        fg_lo = fg & 0xff

        bg_hi = (bg & 0xff00) >> 8
        bg_lo = bg & 0xff

        for character in string:
            try:
                char_index = font.MAP.index(character)
                offset = char_index * font.OFFSET_WIDTH
                bs_bit = font.OFFSETS[offset]
                if font.OFFSET_WIDTH > 1:
                    bs_bit = (bs_bit << 8) + font.OFFSETS[offset + 1]

                if font.OFFSET_WIDTH > 2:
                    bs_bit = (bs_bit << 8) + font.OFFSETS[offset + 2]

                char_width = font.WIDTHS[char_index]
                buffer_needed = char_width * font.HEIGHT * 2

                for i in range(0, buffer_needed, 2):
                    if font.BITMAPS[bs_bit // 8] & 1 << (7 - (bs_bit % 8)) > 0:
                        buffer[i] = fg_hi
                        buffer[i + 1] = fg_lo
                    else:
                        buffer[i] = bg_hi
                        buffer[i + 1] = bg_lo

                    bs_bit += 1

                to_col = x + char_width - 1
                to_row = y + font.HEIGHT - 1
                if self.width > to_col and self.height > to_row:
                    self._set_window(x, y, to_col, to_row)
                    self._write(None, buffer[0:buffer_needed])

                x += char_width

            except ValueError:
                pass

    def write_width(self, font, string):
        """
        Returns the width in pixels of the string if it was written with the
        specified font

        Args:
            font (font): The module containing the converted true-type font
            string (string): The string to measure
        """
        width = 0
        for character in string:
            try:
                char_index = font.MAP.index(character)
                width += font.WIDTHS[char_index]

            except ValueError:
                pass

        return width
