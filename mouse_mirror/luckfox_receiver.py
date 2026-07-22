"""
luckfox_receiver.py  (runs on the Luckfox Pico Ultra W)

A tiny HTTP server that receives raw 240x240 RGB565 frames and blits them to
the GC9A01 display. Pair it with windows_sender.py running on your PC.

Each POST body is exactly 240*240*2 = 115200 bytes of big-endian RGB565 pixel
data (row-major, top-left first) -- the same format the display wants, so the
Luckfox does no decoding at all: it just streams the bytes to the panel.

    PC (windows_sender.py)  --HTTP POST /frame-->  Luckfox (this server)  --> display

Run:
    python3 luckfox_receiver.py --port 8000
    (then enter the Luckfox's IP and this port in the Windows sender)

Find the Luckfox IP with `ifconfig` / `ip addr`.

Wiring (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 60000000

WIDTH = 240
HEIGHT = 240
FRAME_BYTES = WIDTH * HEIGHT * 2

tft = None            # set up in main(), used by the request handler
frames = 0


class FrameHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive so the PC reuses one TCP connection for every
    # frame (much lower latency than reconnecting 20x/second).
    protocol_version = "HTTP/1.1"

    def _reply(self, code=200, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        global frames
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        if len(data) == FRAME_BYTES:
            tft.blit_buffer(data, 0, 0, WIDTH, HEIGHT)
            frames += 1
            self._reply(200, b"ok")
        else:
            self._reply(400, b"expected %d bytes\n" % FRAME_BYTES)

    def do_GET(self):
        self._reply(200, b"gc9a01 frame receiver: POST 240x240 rgb565be to /frame\n")

    def log_message(self, *args):
        pass          # silence per-request logging (would flood at 20 fps)


def main():
    global tft

    parser = argparse.ArgumentParser(description="GC9A01 HTTP frame receiver.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default all)")
    parser.add_argument("--port", type=int, default=8000,
                        help="port to listen on (default 8000)")
    parser.add_argument("--rotation", type=int, default=0,
                        help="display rotation 0-7 (default 0)")
    args = parser.parse_args()

    spi = spidev.SpiDev()
    dc = GPIO(DC_PIN, "out")
    reset = GPIO(RESET_PIN, "out")
    backlight = GPIO(BACKLIGHT_PIN, "out")

    try:
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED_HZ
        spi.mode = 0

        tft = gc9a01.GC9A01(
            spi, dc=dc, reset=reset, backlight=backlight,
            rotation=args.rotation)
        tft.fill(gc9a01.BLACK)

        server = HTTPServer((args.host, args.port), FrameHandler)
        print("Receiver listening on {}:{}  (POST 240x240 rgb565be frames)"
              .format(args.host, args.port))
        print("Point the Windows sender at this device's IP and port.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping ({} frames shown)".format(frames))
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
