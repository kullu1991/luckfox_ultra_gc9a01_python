"""
youtube_video_sender.py  (runs on your PC)

Stream a YouTube video to the Luckfox: video on the GC9A01 display, audio on
the board's codec. Video and audio run as TWO fully independent pipelines so
neither can stall the other:

    VIDEO:  yt-dlp (video stream) -> ffmpeg -> 240x240 rgb565 frames -> HTTP POST
    AUDIO:  yt-dlp (audio stream) -> ffmpeg -> raw PCM -> TCP -> aplay

yt-dlp does the downloading (retrying dropped fragments / throttling), so a
flaky connection is handled gracefully. A reader thread on the video side
drains frames continuously and the HTTP POST runs separately, so a slow POST
drops frames instead of blocking anything -- keeping both smooth.

Setup (once):
    pip install -r requirements.txt      (requests, yt-dlp ; ffmpeg on PATH)
    Keep yt-dlp updated:  pip install -U yt-dlp

On the board, run the video_streamer receiver (video + audio):
    python3 video_streamer/luckfox_receiver.py --port 8000 --audio-port 8001 --volume 15

Run:
    python youtube_video_sender.py 172.32.0.93 8000 "https://www.youtube.com/watch?v=XXXX"
    python youtube_video_sender.py 172.32.0.93 8000 "<url>" --lowest --fps 15
    python youtube_video_sender.py 172.32.0.93 8000 "<url>" --no-audio
"""

import argparse
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

DEVNULL = subprocess.DEVNULL


def ytdlp_cmd(url, fmt):
    """yt-dlp downloading one stream to stdout, with retries."""
    return [
        "yt-dlp", "-q", "--no-warnings", "--no-progress",
        "--retries", "10", "--fragment-retries", "50",
        "-f", fmt, "-o", "-", url,
    ]


def video_ffmpeg_cmd(fps):
    """Read a video stream on stdin, emit 240x240 big-endian RGB565 on stdout."""
    vf = ("scale=240:240:force_original_aspect_ratio=increase:"
          "flags=fast_bilinear,crop=240:240,fps={}".format(fps))
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re", "-i", "pipe:", "-an",
        "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb565be", "pipe:1",
    ]


def audio_ffmpeg_cmd(ip, audio_port):
    """Read an audio stream on stdin, send raw PCM straight to the board TCP."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re", "-i", "pipe:", "-vn",
        "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CH),
        "tcp://{}:{}".format(ip, audio_port),
    ]


def audio_worker(url, afmt, ip, audio_port, loop, stop_event):
    """Independent audio pipeline: yt-dlp | ffmpeg -> tcp. Restarts on --loop."""
    while not stop_event.is_set():
        ydl = ff = None
        try:
            ydl = subprocess.Popen(ytdlp_cmd(url, afmt),
                                   stdout=subprocess.PIPE, stderr=DEVNULL)
            ff = subprocess.Popen(audio_ffmpeg_cmd(ip, audio_port),
                                  stdin=ydl.stdout, stderr=DEVNULL)
            ydl.stdout.close()
            while not stop_event.is_set() and ff.poll() is None:
                time.sleep(0.2)
        except FileNotFoundError:
            return
        finally:
            for p in (ff, ydl):
                if p and p.poll() is None:
                    p.terminate()
            for p in (ff, ydl):
                if p:
                    try:
                        p.wait(timeout=2)
                    except Exception:
                        p.kill()
        if not loop or stop_event.is_set():
            break
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(
        description="Stream a YouTube video to a Luckfox GC9A01 display.")
    parser.add_argument("ip", help="Luckfox IP address")
    parser.add_argument("port", type=int, help="receiver video port")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--fps", type=int, default=15,
                        help="frames per second to send (default 15)")
    parser.add_argument("--max-height", type=int, default=360,
                        help="cap source video height (default 360)")
    parser.add_argument("--lowest", action="store_true",
                        help="use the LOWEST-resolution YouTube streams "
                             "(smallest download, smoothest on a slow link)")
    parser.add_argument("--audio-port", type=int, default=8001,
                        help="receiver audio TCP port (default 8001)")
    parser.add_argument("--no-audio", dest="audio", action="store_false",
                        help="send video only, no audio")
    parser.add_argument("--loop", action="store_true",
                        help="restart when the video ends")
    args = parser.parse_args()

    h = args.max_height
    if args.lowest:
        vfmt = "worstvideo[vcodec!=none]/17/36/18/worst"
        afmt = "worstaudio/bestaudio/worst"
    else:
        vfmt = ("bv[height<=?{h}][vcodec^=avc1]/18/bv[height<=?{h}]"
                "/best[height<=?{h}][acodec!=none][vcodec!=none]".format(h=h))
        afmt = "ba/bestaudio/best"

    post_url = "http://{}:{}/frame".format(args.ip, args.port)
    session = requests.Session()
    print("streaming {} -> {}  at {} fps  (Ctrl+C to stop)".format(
        args.url, post_url, args.fps))

    stop_event = threading.Event()
    if args.audio:
        threading.Thread(
            target=audio_worker,
            args=(args.url, afmt, args.ip, args.audio_port, args.loop,
                  stop_event),
            daemon=True).start()

    sent = 0
    warned = False
    try:
        while True:
            try:
                ydl = subprocess.Popen(ytdlp_cmd(args.url, vfmt),
                                       stdout=subprocess.PIPE)
            except FileNotFoundError:
                print("yt-dlp not found (pip install -U yt-dlp).")
                return
            try:
                ff = subprocess.Popen(video_ffmpeg_cmd(args.fps),
                                      stdin=ydl.stdout, stdout=subprocess.PIPE)
            except FileNotFoundError:
                print("ffmpeg not found on PATH.")
                ydl.terminate()
                return
            ydl.stdout.close()

            # Reader thread drains frames as fast as they arrive, keeping the
            # newest. The POST (variable latency) runs in the main thread, so a
            # slow network drops video frames instead of blocking ffmpeg.
            latest = {"frame": None, "n": 0}
            lock = threading.Lock()
            vstop = threading.Event()

            def reader(pipe=ff.stdout):
                while not vstop.is_set():
                    data = pipe.read(FRAME_BYTES)
                    if len(data) < FRAME_BYTES:
                        break
                    with lock:
                        latest["frame"] = data
                        latest["n"] += 1
                vstop.set()

            rt = threading.Thread(target=reader, daemon=True)
            rt.start()

            fps_count = 0
            fps_start = time.monotonic()
            interval = 1.0 / args.fps
            next_t = time.monotonic()
            last_n = -1
            try:
                while not vstop.is_set():
                    with lock:
                        frame, n = latest["frame"], latest["n"]

                    if frame is not None and n != last_n:
                        last_n = n
                        try:
                            session.post(
                                post_url, data=frame,
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

                    next_t += interval
                    dt = next_t - time.monotonic()
                    if dt > 0:
                        time.sleep(dt)
                    else:
                        next_t = time.monotonic()
            finally:
                vstop.set()
                ff.stdout.close()
                ff.terminate()
                ff.wait()
                ydl.terminate()
                ydl.wait()
                rt.join(timeout=1)

            if not args.loop or stop_event.is_set():
                break
            print("restarting...")
            time.sleep(1)
    finally:
        stop_event.set()

    print("done ({} frames)".format(sent))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
