"""
luckfox_receiver.py  (runs on the Luckfox Pico Ultra W)

Receives video frames over HTTP and (optionally) audio over a second TCP
connection, and plays both on the board: frames go to the GC9A01 display,
audio goes to the built-in codec via aplay. Pair it with video_sender.py (or
windows_sender.py for video only) on your PC.

    PC  --HTTP POST /frame (rgb565)-->  video port  --> GC9A01 display
        --TCP raw PCM ------------->     audio port  --> aplay -> speaker

Video frames: exactly 240*240*2 = 115200 bytes of big-endian RGB565.
Audio: raw signed 16-bit little-endian PCM, 44100 Hz, stereo (what the sender
emits); piped straight into `aplay`.

Run:
    python3 luckfox_receiver.py --port 8000 --audio-port 8001 --volume 15

Find the Luckfox IP with `ip addr`.

Wiring (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import argparse
import os
import socketserver
import subprocess
import sys
import threading
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

# Audio format the sender streams (must match video_sender.py).
AUDIO_DEVICE = "hw:0"
AUDIO_RATE = 44100
AUDIO_CH = 2
# aplay ring-buffer in microseconds: bigger = more tolerant of network jitter
# (fewer underruns) at the cost of a little added audio latency.
AUDIO_BUFFER_US = 500000      # 0.5 s

tft = None            # set up in main(), used by the request handler
frames = 0


# --------------------------------------------------------------------------
# Video: HTTP server, one full frame per POST
# --------------------------------------------------------------------------
class FrameHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # keep-alive: reuse one TCP connection

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
        pass


# --------------------------------------------------------------------------
# Audio: raw TCP stream piped into aplay
# --------------------------------------------------------------------------
class AudioHandler(socketserver.StreamRequestHandler):
    def handle(self):
        print("audio: sender connected")
        try:
            aplay = subprocess.Popen(
                ["aplay", "-q", "-D", AUDIO_DEVICE,
                 "-f", "S16_LE", "-c", str(AUDIO_CH), "-r", str(AUDIO_RATE),
                 "-B", str(AUDIO_BUFFER_US)],
                stdin=subprocess.PIPE)
        except FileNotFoundError:
            print("audio: aplay not found")
            return
        try:
            while True:
                chunk = self.rfile.read(4096)
                if not chunk:
                    break
                aplay.stdin.write(chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            try:
                aplay.stdin.close()
            except Exception:
                pass
            aplay.terminate()
            aplay.wait()
            print("audio: sender disconnected")


class AudioServer(socketserver.TCPServer):
    allow_reuse_address = True
    # single-threaded: one audio sender at a time (serialized), which is what
    # we want (only one aplay writing to the codec).


def set_volume(volume):
    try:
        subprocess.run(
            ["amixer", "cset", "name=DAC LINEOUT Volume", str(volume)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        print("amixer not found; skipping volume set")


def main():
    global tft

    parser = argparse.ArgumentParser(description="GC9A01 HTTP frame + audio receiver.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default all)")
    parser.add_argument("--port", type=int, default=8000,
                        help="video HTTP port (default 8000)")
    parser.add_argument("--audio-port", type=int, default=8001,
                        help="audio TCP port (default 8001; 0 disables audio)")
    parser.add_argument("--volume", type=int, default=None,
                        help="set codec output volume 0-30 at startup")
    parser.add_argument("--rotation", type=int, default=0,
                        help="display rotation 0-7 (default 0)")
    args = parser.parse_args()

    spi = spidev.SpiDev()
    dc = GPIO(DC_PIN, "out")
    reset = GPIO(RESET_PIN, "out")
    backlight = GPIO(BACKLIGHT_PIN, "out")

    audio_server = None
    try:
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED_HZ
        spi.mode = 0

        tft = gc9a01.GC9A01(
            spi, dc=dc, reset=reset, backlight=backlight,
            rotation=args.rotation)
        tft.fill(gc9a01.BLACK)

        if args.volume is not None:
            set_volume(args.volume)

        # Audio TCP server on a background thread (independent of the display).
        if args.audio_port:
            audio_server = AudioServer((args.host, args.audio_port), AudioHandler)
            threading.Thread(target=audio_server.serve_forever,
                             daemon=True).start()
            print("Audio  listening on {}:{}  (raw {} Hz {}ch PCM)".format(
                args.host, args.audio_port, AUDIO_RATE, AUDIO_CH))

        server = HTTPServer((args.host, args.port), FrameHandler)
        print("Video  listening on {}:{}  (POST 240x240 rgb565be frames)".format(
            args.host, args.port))
        print("Point the PC sender at this device's IP.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping ({} frames shown)".format(frames))
    finally:
        if audio_server is not None:
            audio_server.shutdown()
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
