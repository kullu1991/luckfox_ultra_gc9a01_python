"""
luckfox_video.py

Plays a video on the 240x240 circular GC9A01 display.

The key idea: do NOT decode video in Python. Instead let `ffmpeg` decode,
scale to 240x240 and convert to raw RGB565 frames, and pipe those frames to
this script, which only pushes each frame straight to the panel with
blit_buffer(). All the heavy work is native; Python just moves bytes to SPI,
so the SPI clock is the bottleneck (not the decoder).

Audio is played through the Pico Ultra W's built-in codec (ALSA card 0,
device hw:0,0) using a separate ffmpeg -> aplay pipeline that runs in real
time alongside the wall-clock-paced video, so picture and sound stay in sync.
Pass --no-audio for video only. (Audio is only available on the Ultra *W*.)

Requirements on the board:
    - ffmpeg installed and on PATH  (check with: which ffmpeg)
      On the Ubuntu Luckfox image:  sudo apt install ffmpeg
    - aplay + amixer (alsa-utils), already present on the Luckfox image

Usage:
    python3 examples/luckfox_video.py myclip.mp4
    python3 examples/luckfox_video.py myclip.mp4 --fps 24 --rotation 0
    python3 examples/luckfox_video.py myclip.mp4 --volume 20
    python3 examples/luckfox_video.py myclip.mp4 --fps-auto
    python3 examples/luckfox_video.py myclip.mp4 --no-audio

Notes:
    - The panel expects big-endian RGB565, so we ask ffmpeg for rgb565be.
      If colors look wrong (red/blue swapped-ish), change PIX_FMT to
      "rgb565le" below.
    - Playback is anchored to the wall clock (which the audio also follows in
      real time). If the pipeline can't keep up, late frames are DROPPED
      rather than displayed late, so the picture stays in sync with the audio.
    - There is no audio; this is video-only.

Wiring used below (adjust to match your board):
    DC pin        = GPIO 54
    RESET pin     = GPIO 42
    BACKLIGHT pin = GPIO 71
    SPI           = /dev/spidev0.0 (bus 0, device 0)
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spidev
from periphery import GPIO

import gc9a01py_linux as gc9a01

DC_PIN = 54
RESET_PIN = 42
BACKLIGHT_PIN = 71

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 60000000        # 60 MHz gives a higher full-frame ceiling

WIDTH = 240
HEIGHT = 240
PIX_FMT = "rgb565be"           # big-endian to match the driver's '>H' packing
FRAME_BYTES = WIDTH * HEIGHT * 2

# Audio: built-in codec on the Pico Ultra W (ALSA card 0). See
# https://wiki.luckfox.com/Luckfox-Pico-Ultra/Audio
AUDIO_DEVICE = "hw:0"          # aplay -D target
VOLUME_CONTROL = "DAC LINEOUT Volume"   # amixer control, range 0-30

# Re-send the panel config this often so a blank self-heals quickly. The
# heartbeat is ~10 ms (datasheet-minimum Sleep Out settle), so even a short
# interval barely touches the frame budget.
KEEPALIVE_INTERVAL = 2.0       # seconds


def build_ffmpeg_cmd(path, fps, hwdec=False, quality=False,
                     fast_decode=False, skip_frames=False):
    """ffmpeg command: decode -> scale/crop to 240x240 -> raw RGB565 frames.

    Default is the FAST path (cheap bilinear scale, swscale's default light
    dither) which is what a single-core RV1106 can keep up with.

    quality=True switches to lanczos scaling + error-diffusion dither for a
    sharper, less-banded image -- noticeably better looking, but MUCH heavier
    on the CPU (can roughly halve the frame rate). Only use it when your
    source is small enough that decode has headroom to spare.

    fast_decode=True skips the H.264 in-loop deblocking filter (often 30-50%
    of decode time) at the cost of slight blockiness. skip_frames=True also
    skips non-reference frames (choppier motion, more speed). Both are
    decoder options and must precede -i.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    # Decoder-speed options -- must come before -i.
    if fast_decode:
        cmd += ["-skip_loop_filter", "all", "-flags2", "+fast"]
    if skip_frames:
        cmd += ["-skip_frame", "nonref"]
    if quality:
        # Sharper + dithered, but CPU-expensive.
        vf = ("scale=240:240:force_original_aspect_ratio=increase:"
              "flags=lanczos,crop=240:240,fps={},format=rgb565be".format(fps))
        cmd += ["-sws_dither", "ed"]
    else:
        # Fast: fast_bilinear scaler (cheapest), no error-diffusion dither.
        vf = ("scale=240:240:force_original_aspect_ratio=increase:"
              "flags=fast_bilinear,crop=240:240,fps={}".format(fps))
    if hwdec:
        # Use the RV1106's hardware video engine (Rockchip MPP) to decode,
        # freeing the single CPU core. On these builds MPP is a *decoder*
        # (-c:v), not a -hwaccel. `hwdec` is the decoder name, e.g.
        # "h264_rkmpp" or "hevc_rkmpp" -- match it to your source codec.
        # Check what's available:  ffmpeg -hide_banner -decoders | grep rkmpp
        cmd += ["-c:v", hwdec]
    cmd += [
        "-i", path,
        "-vf", vf,
        "-f", "rawvideo",
        "-pix_fmt", PIX_FMT,
        "-",
    ]
    return cmd


PROBE_SECONDS = 2.0            # how long --fps-auto samples decode speed


def probe_fps(path, hwdec, quality, requested, fast_decode=False,
              skip_frames=False):
    """Measure how many 240x240 frames/sec the pipeline can actually produce.

    Runs the real decode pipeline flat-out (no display) for a couple of
    seconds and counts frames. Returns the measured frames/second, or the
    requested fps if ffmpeg can't be started.
    """
    try:
        proc = subprocess.Popen(
            build_ffmpeg_cmd(path, requested, hwdec=hwdec, quality=quality,
                             fast_decode=fast_decode, skip_frames=skip_frames),
            stdout=subprocess.PIPE)
    except FileNotFoundError:
        return float(requested)

    n = 0
    end = time.monotonic() + PROBE_SECONDS
    try:
        while time.monotonic() < end:
            data = proc.stdout.read(FRAME_BYTES)
            if len(data) < FRAME_BYTES:
                break
            n += 1
    finally:
        proc.stdout.close()
        proc.terminate()
        proc.wait()
    return n / PROBE_SECONDS


def set_volume(volume):
    """Best-effort set of the codec output volume (0-30) via amixer."""
    try:
        subprocess.run(
            ["amixer", "cset", "name=" + VOLUME_CONTROL, str(volume)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        print("amixer not found; skipping volume set")


def start_audio(path):
    """Play the file's audio to the codec, cheaply if possible.

    If `path` is already a WAV, aplay it directly -- no ffmpeg, so it costs
    almost no CPU (important on the single-core RV1106). Otherwise fall back
    to decoding on the fly with ffmpeg piped into aplay.

    Returns the list of processes to clean up, or [] if it couldn't start.
    """
    try:
        if path.lower().endswith(".wav"):
            # Direct playback: no runtime audio decode at all.
            ap = subprocess.Popen(["aplay", "-q", "-D", AUDIO_DEVICE, path])
            return [ap]

        ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", path, "-vn", "-f", "wav", "pipe:1"],
            stdout=subprocess.PIPE)
        ap = subprocess.Popen(
            ["aplay", "-q", "-D", AUDIO_DEVICE],
            stdin=ff.stdout)
        ff.stdout.close()          # let aplay own the pipe / receive EOF
        return [ap, ff]
    except FileNotFoundError as exc:
        print("audio disabled ({} not found)".format(exc.filename))
        return []


def stop_audio(procs):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()


def main():
    parser = argparse.ArgumentParser(description="Play a video on the GC9A01.")
    parser.add_argument("video", help="path to the video file")
    parser.add_argument("--fps", type=int, default=24,
                        help="target playback frame rate (default 24)")
    parser.add_argument("--fps-auto", action="store_true",
                        help="probe the source's decode speed at startup and "
                             "set the target fps to what it can actually "
                             "sustain (minimises dropped frames)")
    parser.add_argument("--rotation", type=int, default=0,
                        help="display rotation 0-7 (default 0)")
    parser.add_argument("--loop", action="store_true",
                        help="loop the video forever")
    parser.add_argument("--no-audio", dest="audio", action="store_false",
                        help="play video only, no sound")
    parser.add_argument("--volume", type=int, default=None,
                        help="set codec output volume 0-30 before playback")
    parser.add_argument("--hwdec", nargs="?", const="h264_rkmpp", default=None,
                        help="hardware decoder to use, e.g. h264_rkmpp or "
                             "hevc_rkmpp (default h264_rkmpp if flag given "
                             "with no value). Needs an ffmpeg built with rkmpp.")
    parser.add_argument("--raw", action="store_true",
                        help="the video arg is a raw 240x240 rgb565be frame "
                             "stream (no runtime decode -- fastest). Prepare "
                             "with: ffmpeg -i in.mp4 -vf "
                             "scale=240:240:force_original_aspect_ratio="
                             "increase,crop=240:240,fps=24 -f rawvideo "
                             "-pix_fmt rgb565be out.rgb565")
    parser.add_argument("--audio-file", default=None,
                        help="play audio from this file instead of the video "
                             "arg (use with --raw to keep the original clip's "
                             "sound)")
    parser.add_argument("--keepalive", type=float, default=KEEPALIVE_INTERVAL,
                        help="seconds between keep-awake heartbeats that "
                             "re-send the panel config to stop it blanking "
                             "(0 disables). Default {}".format(
                                 KEEPALIVE_INTERVAL))
    parser.add_argument("--keepalive-full", action="store_true",
                        help="heartbeat also pulses the hardware reset line "
                             "(strongest recovery, brief flash each time)")
    parser.add_argument("--quality", action="store_true",
                        help="sharper lanczos scaling + error-diffusion dither "
                             "(better looking but much heavier on the CPU -- "
                             "can roughly halve the frame rate)")
    parser.add_argument("--fast-decode", action="store_true",
                        help="skip the H.264 in-loop deblocking filter to "
                             "decode faster (slight blockiness)")
    parser.add_argument("--skip-frames", action="store_true",
                        help="also skip non-reference frames for even faster "
                             "decode (choppier motion)")
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print("No such file:", args.video)
        return

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
            rotation=args.rotation)
        tft.fill(gc9a01.BLACK)

        # Pre-set the drawing window once. Every frame is a full-screen
        # 240x240 blit to the same window, so we set it a single time and
        # then only stream pixel data -- no per-frame CASET/RASET commands.
        tft._set_window(0, 0, WIDTH - 1, HEIGHT - 1)

        # Keep-awake needs the newer driver method; if the board still has an
        # old gc9a01py_linux.py, skip the heartbeat rather than crash.
        if args.keepalive and not hasattr(tft, "keep_awake"):
            print("note: driver has no keep_awake(); copy the updated "
                  "gc9a01py_linux.py to the board. Disabling heartbeat.")
            args.keepalive = 0

        # Where the sound comes from: an explicit --audio-file, else the video
        # arg itself (but a raw frame file has no audio track).
        audio_source = args.audio_file or (None if args.raw else args.video)
        if args.audio and audio_source and args.volume is not None:
            set_volume(args.volume)

        # Optionally auto-pick a sustainable fps by probing the decoder.
        if args.fps_auto and not args.raw:
            print("probing decode speed...")
            measured = probe_fps(args.video, args.hwdec, args.quality,
                                 args.fps, fast_decode=args.fast_decode,
                                 skip_frames=args.skip_frames)
            chosen = max(5, min(args.fps, int(measured * 0.95)))
            print("auto fps: source sustains ~{:.1f} fps -> using {} fps"
                  .format(measured, chosen))
            if measured < args.fps * 0.95:
                print("  (source is heavy to decode; a smaller source "
                      "resolution would allow a higher, smoother rate)")
            args.fps = chosen

        frame_interval = 1.0 / args.fps
        shown = 0
        fps_count = 0
        fps_start = time.monotonic()
        last_keepalive = time.monotonic()

        while True:
            # Start audio and video together so they stay roughly in sync;
            # audio plays in real time to the codec, video is paced below.
            audio_procs = (start_audio(audio_source)
                           if args.audio and audio_source else [])

            # Frame source: a raw file (no decode) or an ffmpeg decode pipe.
            proc = None
            if args.raw:
                stream = open(args.video, "rb")
            else:
                proc = subprocess.Popen(
                    build_ffmpeg_cmd(args.video, args.fps, hwdec=args.hwdec,
                                     quality=args.quality,
                                     fast_decode=args.fast_decode,
                                     skip_frames=args.skip_frames),
                    stdout=subprocess.PIPE)
                if proc.poll() is not None:
                    print("ffmpeg failed to start (bad --hwdec decoder?)")
                stream = proc.stdout

            # Anchor the video timeline to the wall clock (which the audio also
            # follows in real time). A frame is dropped only when it is late
            # AND the decoder already had the next frame ready -- i.e. when
            # dropping actually lets us catch up to the audio. If the decoder
            # itself is the bottleneck (each read blocks waiting for a frame,
            # so there is no backlog) we show every frame instead of dropping
            # them all, so a heavy source still plays rather than going blank.
            playback_start = time.monotonic()
            frame_index = 0
            drop_count = 0
            try:
                while True:
                    read_t0 = time.monotonic()
                    data = stream.read(FRAME_BYTES)
                    if len(data) < FRAME_BYTES:
                        break
                    # Fast read => the frame was already queued => backlog.
                    had_backlog = (time.monotonic() - read_t0) < \
                        frame_interval * 0.5

                    target = playback_start + frame_index * frame_interval
                    frame_index += 1
                    now = time.monotonic()
                    lag = now - target

                    if lag > frame_interval and had_backlog:
                        # Late, and more frames are waiting: drop this one so
                        # the picture keeps pace with the audio.
                        drop_count += 1
                    else:
                        if lag < 0:
                            time.sleep(-lag)        # ahead of schedule: wait
                        # Push one full frame: dc high, stream the raw bytes.
                        dc.write(True)
                        spi.writebytes2(data)
                        shown += 1
                        fps_count += 1

                    now = time.monotonic()
                    if now - fps_start >= 1.0:
                        print("playback: {:5.1f} fps  (dropped {})".format(
                            fps_count / (now - fps_start), drop_count))
                        fps_count = 0
                        drop_count = 0
                        fps_start = now

                    # Keep-awake / recovery heartbeat (~10 ms): re-send the full
                    # panel config so it can't stay blanked even if it silently
                    # reset. It issues commands, so re-set the full-screen
                    # window afterwards before the next frame's pixels.
                    if args.keepalive and now - last_keepalive >= args.keepalive:
                        tft.keep_awake(full=args.keepalive_full)
                        tft._set_window(0, 0, WIDTH - 1, HEIGHT - 1)
                        last_keepalive = now
            finally:
                stream.close()
                if proc is not None:
                    proc.wait()
                stop_audio(audio_procs)

            if not args.loop:
                break

        print("done, {} frames".format(shown))
    finally:
        dc.close()
        reset.close()
        backlight.close()
        spi.close()


if __name__ == "__main__":
    main()
