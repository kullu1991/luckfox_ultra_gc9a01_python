"""
video_sender.py  (runs on your PC)

Stream any video to the Luckfox GC9A01 display over HTTP. The PC decodes and
scales the video to 240x240 with ffmpeg; the Luckfox (running
luckfox_receiver.py) just blits the finished frames -- no decoding on the
device, so even heavy sources play smoothly.

    PC: ffmpeg decode+scale -> raw RGB565 frames -> HTTP POST
                                    |
                                    v
    Luckfox: luckfox_receiver.py -> GC9A01 display

The <source> can be anything ffmpeg can open:
    - a local file            movie.mp4
    - an http/https URL       https://.../clip.mp4
    - a network stream        rtsp://...   udp://@:1234   http://<pc>:8080/
      (e.g. VLC's "Stream" output pointed at that URL)

So to use VLC: in VLC choose Media > Stream, output an HTTP or UDP stream,
then run this with that stream URL as <source>.

Setup (once):
    pip install -r requirements.txt      (needs requests; ffmpeg on PATH)

Audio is sent too: a second ffmpeg extracts the source's audio as raw PCM and
streams it over a TCP connection to the receiver's audio port, which plays it
through the Luckfox's built-in codec (aplay). Pass --no-audio for video only.

    PC: ffmpeg -> raw PCM ---TCP---> receiver audio port -> aplay -> speaker

Run:
    python video_sender.py 192.168.1.50 8000 movie.mp4
    python video_sender.py 192.168.1.50 8000 movie.mp4 --fps 24 --loop
    python video_sender.py 192.168.1.50 8000 movie.mp4 --no-audio
    python video_sender.py 192.168.1.50 8000 udp://@:1234 --live   # a VLC stream
"""

import argparse
import socket
import subprocess
import sys
import threading
import time

try:
    import requests
except ImportError:
    print("Missing dependency: requests   (pip install -r requirements.txt)")
    sys.exit(1)

WIDTH = 240
HEIGHT = 240
FRAME_BYTES = WIDTH * HEIGHT * 2

# Audio format streamed to the receiver (must match luckfox_receiver.py).
AUDIO_RATE = 44100
AUDIO_CH = 2


def build_ffmpeg_cmd(source, fps, live=False):
    """Decode <source>, scale/crop to 240x240, emit raw big-endian RGB565.

    live=True omits -re: a live stream (e.g. from VLC) already arrives at
    real-time rate, so pacing it again would build up latency. Use -re only
    for plain files that would otherwise decode as fast as possible.
    """
    vf = ("scale=240:240:force_original_aspect_ratio=increase:"
          "flags=fast_bilinear,crop=240:240,fps={}".format(fps))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    is_rtsp = source.lower().startswith("rtsp://")
    if is_rtsp:
        # A camera stream is always live; TCP transport is reliable over Wi-Fi.
        cmd += ["-rtsp_transport", "tcp", "-fflags", "nobuffer",
                "-flags", "low_delay"]
        live = True

    if not live:
        cmd += ["-re"]         # pace a file at its real-time rate
    cmd += [
        "-i", source,
        "-vf", vf,
        "-f", "rawvideo",
        "-pix_fmt", "rgb565be",
        "-",
    ]
    return cmd


def build_audio_cmd(source, live=False):
    """Decode <source>'s audio to raw S16LE PCM at AUDIO_RATE/AUDIO_CH."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    is_rtsp = source.lower().startswith("rtsp://")
    if is_rtsp:
        cmd += ["-rtsp_transport", "tcp"]
        live = True
    if not live:
        cmd += ["-re"]
    cmd += [
        "-i", source,
        "-vn",
        "-f", "s16le",
        "-ar", str(AUDIO_RATE),
        "-ac", str(AUDIO_CH),
        "-",
    ]
    return cmd


def audio_worker(source, ip, audio_port, live, loop, stop_event):
    """Stream the source's audio as raw PCM to the receiver's audio TCP port.

    Runs in a background thread. aplay on the Luckfox consumes at real time,
    so the socket send naturally throttles to real time (keeping A/V roughly
    in sync). Retries/loops alongside the video.
    """
    while not stop_event.is_set():
        try:
            proc = subprocess.Popen(build_audio_cmd(source, live=live),
                                    stdout=subprocess.PIPE)
        except FileNotFoundError:
            print("audio: ffmpeg not found.")
            return

        try:
            with socket.create_connection((ip, audio_port), timeout=5) as sock:
                while not stop_event.is_set():
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break                      # source ended / no audio
                    sock.sendall(chunk)
        except OSError as exc:
            print("audio: connection failed ({})".format(exc))
        finally:
            proc.stdout.close()
            proc.terminate()
            proc.wait()

        if not loop or stop_event.is_set():
            break
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(
        description="Stream a video to a Luckfox GC9A01 display over HTTP.")
    parser.add_argument("ip", help="Luckfox IP address")
    parser.add_argument("port", type=int, help="Luckfox receiver port")
    parser.add_argument("source", help="video file, URL or network stream")
    parser.add_argument("--fps", type=int, default=20,
                        help="frames per second to send (default 20)")
    parser.add_argument("--loop", action="store_true",
                        help="restart the source when it ends")
    parser.add_argument("--live", action="store_true",
                        help="source is a live stream (e.g. from VLC); omit "
                             "ffmpeg's -re real-time pacing")
    parser.add_argument("--no-audio", dest="audio", action="store_false",
                        help="send video only, no audio")
    parser.add_argument("--audio-port", type=int, default=8001,
                        help="receiver audio TCP port (default 8001)")
    args = parser.parse_args()

    url = "http://{}:{}/frame".format(args.ip, args.port)
    session = requests.Session()

    print("Streaming {} -> {}  at {} fps  (Ctrl+C to stop)"
          .format(args.source, url, args.fps))

    # Start the audio stream in the background (plays via aplay on the board).
    stop_event = threading.Event()
    audio_thread = None
    if args.audio:
        audio_thread = threading.Thread(
            target=audio_worker,
            args=(args.source, args.ip, args.audio_port, args.live, args.loop,
                  stop_event),
            daemon=True)
        audio_thread.start()

    sent = 0
    warned = False

    try:
        while True:
            try:
                proc = subprocess.Popen(
                    build_ffmpeg_cmd(args.source, args.fps, live=args.live),
                    stdout=subprocess.PIPE)
            except FileNotFoundError:
                print("ffmpeg not found on PATH.")
                return

            fps_count = 0
            fps_start = time.monotonic()
            try:
                while True:
                    data = proc.stdout.read(FRAME_BYTES)
                    if len(data) < FRAME_BYTES:
                        break                      # source ended

                    try:
                        session.post(
                            url, data=data,
                            headers={"Content-Type": "application/octet-stream"},
                            timeout=2)
                        sent += 1
                        fps_count += 1
                        warned = False
                    except requests.RequestException as exc:
                        if not warned:
                            print("send failed ({}); retrying...".format(exc))
                            warned = True
                        time.sleep(0.5)

                    now = time.monotonic()
                    if now - fps_start >= 1.0:
                        print("sent {:5.1f} fps".format(
                            fps_count / (now - fps_start)))
                        fps_count = 0
                        fps_start = now
            finally:
                proc.stdout.close()
                proc.terminate()
                proc.wait()

            if not args.loop:
                break
    finally:
        stop_event.set()      # tell the audio thread to stop

    print("done ({} frames)".format(sent))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
