"""Loudred - loudness-triggered screen+audio clipper (instant replay).

Continuously buffers screen video + desktop/Discord audio (loopback) + mic into
1s segments. When the mic's RMS loudness crosses a threshold, saves the window
(15s before + 15s after) as one mp4; if loudness keeps coming the clip extends
to keep covering it (up to MAX_EXTEND).

Needs ffmpeg on PATH. See README.md.
"""
import argparse
import ctypes
import glob
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes
from datetime import datetime

import numpy as np
import sounddevice as sd

SEG_LEN = 1                  # seconds per segment
PRE = 15                     # seconds before the peak
POST = 15                    # seconds after the peak
RETENTION = 60              # keep this many seconds of buffer on disk
MAX_EXTEND = 20             # if peaks keep coming, grow a clip's end by up to this
                            # many seconds (back-to-back moments -> one clip).
                            # ponytail: ceiling is RETENTION-PRE-POST-SEG_LEN (~29s);
                            # raise RETENTION before raising this.
FPS = 30
BUFDIR = "buffer"
CLIPDIR = "clips"
QUALITY_PRESETS = {"Native": None, "1080p": 1080, "720p": 720, "480p": 480}
AUDIO_SR = 48000            # desktop-loopback PCM piped to ffmpeg
AUDIO_CH = 2
AUDIO_FMT = "f32le"         # soundcard yields float32 frames
ENCODER_LABELS = {"CPU (x264)": "libx264", "AMD GPU (AMF)": "h264_amf",
                  "NVIDIA GPU (NVENC)": "h264_nvenc", "Intel GPU (QSV)": "h264_qsv"}
BUFFER_LABELS = {"RAM (in memory, no SSD writes)": "ram", "Disk folder": "disk"}
SEG_GLOB = "seg_*.ts"
SEG_FMT = "seg_%Y%m%d_%H%M%S.ts"
LOOPBACK_HINTS = ("stereo mix", "cable output", "loopback", "what u hear",
                  "what you hear", "voicemeeter out b1")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "loudred_settings.json")


# ---- pure logic (covered by test_app.py) ----

def rms_level(block):
    """RMS loudness of a float32 block, 0..1.

    RMS, not peak: a single stray sample (a click/pop) gets averaged away, so
    only sustained loudness trips the trigger.
    ponytail: per-callback RMS, no smoothing. Add a moving average across blocks
    only if one noisy block still false-fires.
    """
    if len(block) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


def load_settings():
    """GUI choices from last run (or {} if missing/unreadable)."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def parse_seg_time(name):
    """Epoch seconds from a seg_YYYYmmdd_HHMMSS.ts filename (local time)."""
    base = os.path.basename(name)
    return datetime.strptime(base, SEG_FMT).timestamp()


def select_range(segs, lo, hi, seg_len=SEG_LEN):
    """Pick segments overlapping the epoch-second window [lo, hi].

    segs: list of (name, start_epoch). Returns names sorted by start time.
    """
    chosen = [(s, n) for n, s in segs if s + seg_len >= lo and s <= hi]
    return [n for _, n in sorted(chosen)]


def select_segments(segs, trigger, pre=PRE, post=POST, seg_len=SEG_LEN):
    """Segments overlapping [trigger-pre, trigger+post] (single-peak window)."""
    return select_range(segs, trigger - pre, trigger + post, seg_len)


# ---- device discovery ----

def list_dshow_audio():
    """Names of DirectShow audio devices via ffmpeg (parsed from stderr)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True,
        ).stderr
    except FileNotFoundError:
        return []          # ffmpeg not installed; main() shows a friendly error
    # ffmpeg tags each device line "(audio)"/"(video)"; skip "Alternative name" lines.
    names = []
    for line in out.splitlines():
        if line.rstrip().endswith("(audio)") and '"' in line and "Alternative name" not in line:
            names.append(line.split('"')[1])
    return names


def pick_devices(mic_override=None, loop_override=None, names=None):
    """Return (mic_name, loopback_name_or_None). Pass `names` to reuse an
    already-fetched device list and avoid a second ffmpeg launch."""
    if names is None:
        names = list_dshow_audio()
    loop = loop_override or next(
        (n for n in names if any(h in n.lower() for h in LOOPBACK_HINTS)), None)
    mic = mic_override or next((n for n in names if n != loop), None)
    return mic, loop


def list_input_mics():
    """(index, name) for sounddevice input-capable devices (for the trigger)."""
    try:
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0]
    except Exception:
        return []


def have_soundcard():
    """True if the optional `soundcard` lib (WASAPI loopback) is importable."""
    try:
        import soundcard  # noqa: F401
        return True
    except Exception:
        return False


def capture_loopback(write, stop, on_event=lambda m: None):
    """Capture the default speaker's WASAPI loopback and push float32 PCM to
    `write` until `stop` is set. This is desktop audio (game + Discord + browser)
    without Stereo Mix or a virtual cable - Windows' native per-endpoint loopback.
    Runs in its own thread; any failure is reported and ends the thread."""
    try:
        import soundcard as sc
    except ImportError:
        on_event("Desktop audio needs 'soundcard' (pip install soundcard). Skipping.")
        return
    try:
        spk = sc.default_speaker()
        mic = sc.get_microphone(spk.name, include_loopback=True)
        with mic.recorder(samplerate=AUDIO_SR, channels=AUDIO_CH) as rec:
            on_event("Desktop audio: capturing '%s' via WASAPI loopback." % spk.name)
            while not stop.is_set():
                data = rec.record(numframes=2048)        # (frames, channels) float32
                try:
                    write(np.ascontiguousarray(data, dtype=np.float32).tobytes())
                except (OSError, ValueError):
                    break                                # ffmpeg/pipe gone
    except Exception as e:
        on_event("Desktop audio capture failed: %s" % e)


def set_dpi_aware():
    """Make this process per-monitor DPI aware so monitor rects are in physical
    pixels and line up with what gdigrab actually captures under display scaling."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()     # fallback (older Windows)
        except Exception:
            pass


def list_monitors():
    """[(left, top, width, height, is_primary)] per monitor (Windows). Primary at (0,0)."""
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return []
    out = []
    cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

    def cb(hmon, hdc, lprc, lparam):
        r = lprc.contents
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                    r.left == 0 and r.top == 0))
        return True

    user32.EnumDisplayMonitors(None, None, cb_t(cb), 0)
    return out


# ---- continuous recorder + pruner ----

def build_ffmpeg_cmd(audio_devices, region=None, scale_h=None, bufdir=BUFDIR,
                     ram=False, encoder="libx264", desktop_audio=False):
    """ffmpeg cmd: screen video + any number of dshow audio sources mixed to one track.

    region   = (x, y, w, h) crops gdigrab to one monitor; None = whole virtual desktop.
    scale_h  = downscale video to this many lines (keeps aspect, even width);
               None = capture at native resolution.
    bufdir   = directory the 1s segments are written to (disk-buffer mode only).
    ram      = True -> fragmented MP4 to stdout for an in-RAM ring buffer (no disk);
               False -> 1s .ts segments written to bufdir.
    encoder  = video encoder id (libx264 / h264_amf / h264_nvenc / h264_qsv).
    desktop_audio = True -> read WASAPI-loopback PCM from stdin (pipe:0) as one more
               audio source (desktop sound without Stereo Mix / a virtual cable).
    """
    cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(FPS)]
    if region:
        x, y, w, h = region
        cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", "%dx%d" % (w, h)]
    cmd += ["-i", "desktop"]
    for d in audio_devices:
        cmd += ["-f", "dshow", "-i", "audio=" + d]
    n = len(audio_devices)
    audio_idx = list(range(1, n + 1))               # dshow input indices
    if desktop_audio:                               # loopback PCM on stdin = next index
        cmd += ["-f", AUDIO_FMT, "-ar", str(AUDIO_SR), "-ac", str(AUDIO_CH), "-i", "pipe:0"]
        audio_idx.append(n + 1)

    # Build one filtergraph for whatever needs filtering (video scale, audio mix);
    # map everything else straight through. -vf can't coexist with -filter_complex,
    # so scaling goes through filter_complex too.
    chains, maps = [], []
    if scale_h:
        chains.append("[0:v]scale=-2:%d[v]" % scale_h)
        maps += ["-map", "[v]"]
    else:
        maps += ["-map", "0:v"]
    if len(audio_idx) == 1:
        maps += ["-map", "%d:a" % audio_idx[0]]
    elif len(audio_idx) >= 2:
        ins = "".join("[%d:a]" % i for i in audio_idx)
        chains.append("%samix=inputs=%d:duration=longest[a]" % (ins, len(audio_idx)))
        maps += ["-map", "[a]"]
    if chains:
        cmd += ["-filter_complex", ";".join(chains)]
    cmd += maps
    cmd += _venc_args(encoder)
    cmd += ["-c:a", "aac"]
    if ram:
        # Fragmented MP4 to stdout: each fragment is a self-contained,
        # keyframe-aligned moof+mdat, so a RAM ring buffer can keep/drop whole
        # fragments and concat init+fragments into a playable clip - no disk.
        cmd += ["-g", str(FPS),
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", "pipe:1"]
    else:
        cmd += ["-f", "segment", "-segment_time", str(SEG_LEN),
                "-reset_timestamps", "1", "-strftime", "1",
                os.path.join(bufdir, SEG_FMT)]
    return cmd


def _venc_args(encoder):
    """Video-encoder ffmpeg args. GPU encoders (AMF/NVENC/QSV) offload from the
    CPU the way ReLive/ShadowPlay do; they don't shrink disk/RAM use (bitrate is
    similar). If the chosen encoder isn't built into the user's ffmpeg, ffmpeg
    exits and _watch_proc surfaces it."""
    if encoder == "h264_amf":           # AMD / Radeon
        return ["-c:v", "h264_amf", "-usage", "lowlatency", "-quality", "speed"]
    if encoder == "h264_nvenc":         # NVIDIA
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll"]
    if encoder == "h264_qsv":           # Intel
        return ["-c:v", "h264_qsv", "-preset", "veryfast"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


# Rough H.264-ultrafast bits per pixel per frame. Content-dependent (motion,
# detail), so footprint estimates are an order-of-magnitude guide, not a promise.
BITS_PER_PIXEL = 0.10


def estimate_footprint(width, height, fps, buffer_secs, clip_secs):
    """Rough resource estimate for capturing WxH@fps. Returns a dict of:
      mbps      - encoded video bitrate (Mbit/s)
      buffer_mb - rolling buffer size (buffer_secs of video) - lives in RAM or on
                  disk depending on buffer mode; either way this is what grows
      clip_mb   - one saved clip on disk (clip_secs of video)
      ram_mb    - rough ffmpeg+python working set, excluding the RAM-mode buffer
    """
    bps = width * height * fps * BITS_PER_PIXEL          # bits/sec
    raw_frame_mb = width * height * 1.5 / 1e6            # yuv420 bytes/frame
    return {
        "mbps": bps / 1e6,
        "buffer_mb": bps * buffer_secs / 8 / 1e6,
        "clip_mb": bps * clip_secs / 8 / 1e6,
        "ram_mb": 150 + raw_frame_mb * 8,               # ~base + a few frames in flight
    }


def prune_loop(stop, retention=RETENTION, bufdir=BUFDIR):
    """Delete buffer segments (and stale concat lists) older than `retention` secs."""
    while not stop.is_set():
        cutoff = time.time() - retention
        for f in glob.glob(os.path.join(bufdir, SEG_GLOB)):
            try:
                if parse_seg_time(f) < cutoff:
                    os.remove(f)
            except (ValueError, OSError):
                pass
        # concat_*.txt lives <post+1s during a clip; anything older is orphaned
        # by a crash between write and remove in build_clip.
        for f in glob.glob(os.path.join(bufdir, "concat_*.txt")):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except OSError:
                pass
        stop.wait(5)


def build_clip(first_peak, last_peak, log, clipdir=CLIPDIR, pre=PRE, post=POST, bufdir=BUFDIR):
    """Concat the buffered segments around a loud moment into one mp4.

    Window = [first_peak-pre, last_peak+post]. With no extra peaks
    last_peak == first_peak (the usual pre+post seconds); when loudness kept
    coming the window stretches to cover it all. Lossless stream copy.
    """
    segs = []
    for f in glob.glob(os.path.join(bufdir, SEG_GLOB)):
        try:
            segs.append((f, parse_seg_time(f)))
        except ValueError:
            pass
    chosen = select_range([(f, t) for f, t in segs], first_peak - pre, last_peak + post)
    if not chosen:
        log("No segments to clip (buffer empty?)")
        return None
    listfile = os.path.join(bufdir, "concat_%d.txt" % int(first_peak))
    with open(listfile, "w") as fh:
        for n in chosen:
            fh.write("file '%s'\n" % os.path.abspath(n).replace("\\", "/"))
    os.makedirs(clipdir, exist_ok=True)
    out = os.path.join(clipdir,
                       "clip_%s.mp4" % datetime.fromtimestamp(first_peak).strftime("%Y%m%d_%H%M%S"))
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0",
         "-i", listfile, "-c", "copy", out],
        capture_output=True, text=True)
    try:
        os.remove(listfile)
    except OSError:
        pass
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines() if r.stderr else []
        log("Clip failed: " + (tail[-1] if tail else "ffmpeg error"))
        return None
    log("Saved " + out)
    return out


def iter_mp4_boxes(read):
    """Yield (type, raw_bytes) for each top-level MP4 box read from `read` (a
    file.read-style callable returning b'' at EOF). Box = 4-byte big-endian size
    + 4-byte type [+ 8-byte largesize if size==1] + payload."""
    buf = b""

    def fill(n):
        nonlocal buf
        while len(buf) < n:
            chunk = read(65536)
            if not chunk:
                return False
            buf += chunk
        return True

    while True:
        if not fill(8):
            return
        size = int.from_bytes(buf[:4], "big")
        btype = buf[4:8].decode("latin1", "replace")
        if size == 1:                       # 64-bit largesize
            if not fill(16):
                return
            size = int.from_bytes(buf[8:16], "big")
        if size < 8:                        # 0 (= to EOF) or garbage: bail safely
            return
        if not fill(size):
            return
        yield btype, buf[:size]
        buf = buf[size:]


class RamBuffer:
    """Rolling in-RAM ring buffer of fragmented-MP4 fragments (the ShadowPlay/
    ReLive approach: keep the last `retention` seconds of encoded video in memory,
    write to disk only when a clip is saved - no continuous SSD writes)."""

    def __init__(self, retention):
        self.retention = retention
        self.init = b""                     # ftyp + moov (the init segment)
        self.frags = deque()                # (arrival_epoch, moof+mdat bytes)
        self._lock = threading.Lock()

    def feed_stream(self, read, stop):
        """Consume ffmpeg's fMP4 stdout until EOF/stop, indexing fragments by
        wall-clock arrival (~media time within sub-second, matching clip precision)."""
        init_parts, pending, have_moof = [], b"", False
        for btype, data in iter_mp4_boxes(read):
            if stop.is_set():
                break
            if not self.init:
                init_parts.append(data)
                if btype == "moov":
                    with self._lock:
                        self.init = b"".join(init_parts)
                continue
            if btype == "styp":
                pending = data
            elif btype == "moof":
                pending += data
                have_moof = True
            elif btype == "mdat" and have_moof:
                now = time.time()
                with self._lock:
                    self.frags.append((now, pending + data))
                    self._prune(now)
                pending, have_moof = b"", False

    def _prune(self, now):
        cutoff = now - self.retention
        while self.frags and self.frags[0][0] < cutoff:
            self.frags.popleft()

    def slice(self, lo, hi):
        """(init, [fragment bytes]) for fragments that arrived within [lo, hi]."""
        with self._lock:
            return self.init, [b for (t, b) in self.frags if lo <= t <= hi]


class Clipper:
    """Owns the ffmpeg recorder, pruner, mic-trigger and clip building."""

    def __init__(self, threshold, audio_devices, trigger_mic=None, on_event=print,
                 clipdir=CLIPDIR, test_mode=False, region=None,
                 pre=PRE, post=POST, scale_h=None, bufdir=BUFDIR,
                 buffer_mode="ram", encoder="libx264", desktop_audio=False):
        self.threshold = threshold
        self.audio_devices = list(audio_devices)   # dshow names recorded into the clip
        self.trigger_mic = trigger_mic             # sounddevice index/name; None = default
        self.region = region                       # (x,y,w,h) monitor crop; None = all
        self.pre = pre                             # seconds kept before a peak
        self.post = post                           # seconds kept after a peak
        self.scale_h = scale_h                     # downscale height; None = native
        self.bufdir = bufdir                       # disk-buffer folder (disk mode only)
        self.buffer_mode = buffer_mode             # "ram" (no SSD writes) | "disk"
        self.encoder = encoder                     # libx264 / h264_amf / h264_nvenc / h264_qsv
        self.desktop_audio = desktop_audio         # WASAPI loopback PCM on stdin (no cable)
        # buffer must outlast the longest possible clip (window + extension + margin)
        self.retention = pre + post + MAX_EXTEND + 10
        self.on_event = on_event
        self.clipdir = clipdir
        self.test_mode = test_mode
        self.level = 0.0           # latest mic RMS loudness, for the GUI meter
        self.clip_count = 0
        self.state = "idle"        # idle | armed | capturing | cooldown | error
        self._proc = None
        self._errlog = None
        self._logpath = "ffmpeg.log"
        self._rambuf = None        # RamBuffer when buffer_mode == "ram"
        self._stop = threading.Event()
        self._stream = None
        self._armed = True
        self._first_peak = 0.0     # window anchors for the in-flight clip
        self._last_peak = 0.0      # pushed out by later peaks (capped at MAX_EXTEND)
        self._lock = threading.Lock()

    def start(self):
        ram = self.buffer_mode == "ram"
        os.makedirs(self.clipdir, exist_ok=True)
        if ram:
            self._logpath = os.path.join(os.path.dirname(SETTINGS_FILE), "ffmpeg.log")
        else:
            os.makedirs(self.bufdir, exist_ok=True)
            self._logpath = os.path.join(self.bufdir, "ffmpeg.log")
        if not self.audio_devices:
            self.on_event("WARNING: no audio sources selected - clips will be video-only.")
        vid = ("monitor %dx%d at (%d,%d)" % (self.region[2], self.region[3],
               self.region[0], self.region[1])) if self.region else "ALL monitors (whole desktop)"
        if self.scale_h:
            vid += " -> scaled to %dp" % self.scale_h
        self.on_event("Video: %s  [%s]" % (vid, self.encoder))
        srcs = list(self.audio_devices) + (["Desktop audio (loopback)"] if self.desktop_audio else [])
        self.on_event("Recording sources: " + (", ".join(srcs) or "NONE"))
        self.on_event("Buffer: %s (%ds rolling)"
                      % ("RAM (in memory)" if ram else os.path.abspath(self.bufdir),
                         self.retention))
        self._stop.clear()
        # ponytail: mic opened twice (ffmpeg records, sounddevice triggers).
        # Windows shared-mode allows this; if it ever fails, parse ffmpeg
        # silencedetect output for the trigger instead.
        try:
            self._stream = sd.InputStream(device=self.trigger_mic, channels=1,
                                          callback=self._on_audio)
            self._stream.start()
        except Exception as e:
            self.on_event("Trigger mic failed to open: %s" % e)
            return False
        self._errlog = open(self._logpath, "w")
        cmd = build_ffmpeg_cmd(self.audio_devices, self.region, self.scale_h,
                               self.bufdir, ram=ram, encoder=self.encoder,
                               desktop_audio=self.desktop_audio)
        out = subprocess.PIPE if ram else subprocess.DEVNULL
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                      stdout=out, stderr=self._errlog)
        if ram:
            self._rambuf = RamBuffer(self.retention)
            threading.Thread(target=self._rambuf.feed_stream,
                             args=(self._proc.stdout.read, self._stop), daemon=True).start()
        else:
            threading.Thread(target=prune_loop,
                             args=(self._stop, self.retention, self.bufdir), daemon=True).start()
        if self.desktop_audio:               # feed loopback PCM into ffmpeg's stdin
            threading.Thread(target=capture_loopback,
                             args=(self._proc.stdin.write, self._stop, self.on_event),
                             daemon=True).start()
        threading.Thread(target=self._watch_proc, daemon=True).start()
        self.state = "armed"
        self.on_event("Recording. Buffering %ds; clips = %ds before + %ds after a peak.%s"
                      % (self.retention, self.pre, self.post,
                         "  [TEST MODE - no clips]" if self.test_mode else ""))
        return True

    def _watch_proc(self):
        """If ffmpeg dies on its own (bad device, dshow error), say so loudly
        instead of pretending we're still recording."""
        proc = self._proc
        if not proc:
            return
        code = proc.wait()
        if not self._stop.is_set():
            self.state = "error"
            self.on_event("Recorder (ffmpeg) stopped unexpectedly (exit %s). See %s"
                          % (code, self._logpath))

    def _on_audio(self, indata, frames, t, status):
        lvl = rms_level(indata[:, 0])
        self.level = lvl
        if self.test_mode:
            return
        if lvl >= self.threshold:
            now = time.time()
            with self._lock:
                if self._armed:
                    self._armed = False
                    self.state = "capturing"
                    self._first_peak = self._last_peak = now
                    threading.Thread(target=self._do_clip, daemon=True).start()
                elif self.state == "capturing":
                    # still loud mid-capture: push the clip's end out (capped) so
                    # back-to-back moments land in one clip instead of being dropped.
                    self._last_peak = min(now, self._first_peak + MAX_EXTEND)

    def _do_clip(self):
        self.on_event("Peak! Capturing window...")
        # Wait until POST seconds past the LAST peak. _on_audio keeps pushing
        # _last_peak out (up to MAX_EXTEND) while loudness continues, so a burst
        # of moments extends one clip instead of being dropped during capture.
        while True:
            with self._lock:
                deadline = self._last_peak + self.post + 1
            if time.time() >= deadline:
                break
            time.sleep(0.25)
        with self._lock:
            first, last = self._first_peak, self._last_peak
        if last > first:
            self.on_event("Loud streak: extended clip to %ds." % int(last - first + self.pre + self.post))
        if self.buffer_mode == "ram":
            out = self._save_ram_clip(first, last)
        else:
            out = build_clip(first, last, self.on_event, self.clipdir,
                             self.pre, self.post, self.bufdir)
        if out:
            self.clip_count += 1
        self.state = "cooldown"
        time.sleep(1)                   # brief cooldown
        with self._lock:
            self._armed = True
            self.state = "armed"

    def _save_ram_clip(self, first, last):
        """Write a clip straight from the RAM ring buffer: init segment + the
        fragments inside [first-pre, last+post]. The fragments are keyframe-aligned
        fMP4, so init + fragments is already a playable file - no remux."""
        init, frags = self._rambuf.slice(first - self.pre, last + self.post)
        if not init or not frags:
            self.on_event("No buffered video to clip yet.")
            return None
        os.makedirs(self.clipdir, exist_ok=True)
        out = os.path.join(self.clipdir,
                           "clip_%s.mp4" % datetime.fromtimestamp(first).strftime("%Y%m%d_%H%M%S"))
        try:
            with open(out, "wb") as f:
                f.write(init)
                for fr in frags:
                    f.write(fr)
        except OSError as e:
            self.on_event("Clip write failed: %s" % e)
            return None
        self.on_event("Saved " + out)
        return out

    def stop(self):
        self._stop.set()
        if self._stream:
            self._stream.stop(); self._stream.close(); self._stream = None
        if self._proc:
            try:
                if self.desktop_audio:
                    # stdin is the PCM feed, not the command channel - the capture
                    # thread already stopped (self._stop set); just close + terminate.
                    self._proc.stdin.close()
                    self._proc.terminate()
                else:
                    self._proc.stdin.write(b"q")   # ask ffmpeg to finalize segments
                    self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.terminate()
            self._proc = None
        if self._errlog:
            self._errlog.close(); self._errlog = None
        self.state = "idle"
        self.level = 0.0
        self.on_event("Stopped.")


# ---- GUI ----

def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog

    root = tk.Tk()
    root.title("Loudred - instant replay clipper")
    root.geometry("560x980")

    # red "record" dot as the window/taskbar icon (no file, no extra dependency)
    icon = tk.PhotoImage(width=64, height=64)
    icon.put("#2b2b2b", to=(0, 0, 64, 64))
    cx = cy = 32
    r = 22
    for y in range(64):
        dy = y - cy
        if abs(dy) <= r:
            dx = int((r * r - dy * dy) ** 0.5)
            icon.put("#e74c3c", to=(cx - dx, y, cx + dx, y + 1))
    root._icon = icon            # keep a reference so it isn't garbage-collected
    root.iconphoto(True, icon)

    cfg = load_settings()
    status = tk.StringVar(value="Idle. Pick devices, set threshold, then Start.")
    folder = tk.StringVar(value=cfg.get("folder", os.path.abspath(CLIPDIR)))
    test_var = tk.BooleanVar(value=False)
    clipper = {"obj": None}

    def log(msg):
        status.set(msg)
        listbox.insert(tk.END, time.strftime("%H:%M:%S  ") + msg)
        listbox.see(tk.END)

    # --- screen / monitor to capture ---
    monitors = list_monitors()
    mon_opts = ["All monitors (whole desktop)"]
    default_mon = 0
    for i, (x, y, w, h, prim) in enumerate(monitors):
        mon_opts.append("Monitor %d - %dx%d at (%d,%d)%s"
                        % (i + 1, w, h, x, y, "  [primary]" if prim else ""))
        if prim:
            default_mon = i + 1            # default to the primary monitor
    ttk.Label(root, text="Record video from (screen capture, H.264):").pack(anchor="w", padx=10, pady=(10, 0))
    mon_cb = ttk.Combobox(root, state="readonly", values=mon_opts)
    saved_mon = cfg.get("monitor", default_mon)
    mon_cb.current(saved_mon if 0 <= saved_mon < len(mon_opts)
                   else (default_mon if mon_opts else 0))
    mon_cb.pack(fill="x", padx=10)
    mon_cb.bind("<<ComboboxSelected>>", lambda e: _update_stats())

    # --- capture quality (downscale to spare CPU + buffer size) ---
    ttk.Label(root, text="Capture quality:").pack(anchor="w", padx=10, pady=(8, 0))
    quality_cb = ttk.Combobox(root, state="readonly", values=list(QUALITY_PRESETS))
    quality_cb.set(cfg.get("quality") if cfg.get("quality") in QUALITY_PRESETS else "Native")
    quality_cb.pack(fill="x", padx=10)
    quality_cb.bind("<<ComboboxSelected>>", lambda e: _update_stats())

    # --- clip window (seconds before / after the peak) ---
    win = ttk.Frame(root); win.pack(fill="x", padx=10, pady=(8, 0))
    ttk.Label(win, text="Clip window  -  seconds before:").pack(side="left")
    pre_var = tk.IntVar(value=int(cfg.get("pre", PRE)))
    pre_spin = ttk.Spinbox(win, from_=1, to=120, width=4, textvariable=pre_var,
                           command=lambda: _update_stats())
    pre_spin.pack(side="left", padx=(4, 10))
    ttk.Label(win, text="after:").pack(side="left")
    post_var = tk.IntVar(value=int(cfg.get("post", POST)))
    post_spin = ttk.Spinbox(win, from_=1, to=120, width=4, textvariable=post_var,
                            command=lambda: _update_stats())
    post_spin.pack(side="left", padx=4)

    # --- video encoder (GPU offloads encoding from the CPU, ReLive-style) ---
    ttk.Label(root, text="Video encoder:").pack(anchor="w", padx=10, pady=(8, 0))
    enc_cb = ttk.Combobox(root, state="readonly", values=list(ENCODER_LABELS))
    enc_cb.set(next((lbl for lbl, eid in ENCODER_LABELS.items()
                     if eid == cfg.get("encoder")), "CPU (x264)"))
    enc_cb.pack(fill="x", padx=10)

    # --- rolling buffer: RAM by default (no SSD writes), or a disk folder ---
    ttk.Label(root, text="Rolling buffer:").pack(anchor="w", padx=10, pady=(8, 0))
    bufmode_cb = ttk.Combobox(root, state="readonly", values=list(BUFFER_LABELS))
    bufmode_cb.set(next((lbl for lbl, m in BUFFER_LABELS.items()
                         if m == cfg.get("buffer_mode")), "RAM (in memory, no SSD writes)"))
    bufmode_cb.pack(fill="x", padx=10)
    bufmode_cb.bind("<<ComboboxSelected>>", lambda e: (_sync_buf_entry(), _update_stats()))
    buf_var = tk.StringVar(value=cfg.get("bufdir", BUFDIR))
    buf_entry = ttk.Entry(root, textvariable=buf_var)
    buf_entry.pack(fill="x", padx=10)
    buf_var.trace_add("write", lambda *a: _update_stats())
    if BUFFER_LABELS.get(bufmode_cb.get()) != "disk":
        buf_entry.config(state="disabled")     # folder only matters in disk mode

    # --- stats for nerds ---
    stats_var = tk.BooleanVar(value=bool(cfg.get("stats", False)))
    ttk.Checkbutton(root, text="Stats for nerds (estimate buffer / clip footprint)",
                    variable=stats_var,
                    command=lambda: _update_stats()).pack(anchor="w", padx=10, pady=(8, 0))
    stats_lbl = ttk.Label(root, text="", foreground="#3498db")
    stats_lbl.pack(anchor="w", padx=10)

    # --- trigger mic (sounddevice) ---
    mics = list_input_mics()
    ttk.Label(root, text="Trigger mic (its loudness arms a clip):").pack(anchor="w", padx=10, pady=(10, 0))
    mic_cb = ttk.Combobox(root, state="readonly",
                          values=[name for _, name in mics] or ["(no input devices)"])
    mic_cb.current(0)
    if cfg.get("mic") in mic_cb["values"]:
        mic_cb.set(cfg["mic"])
    mic_cb.pack(fill="x", padx=10)

    # --- recorded audio sources (dshow, multi-select) ---
    dshow = list_dshow_audio()
    ttk.Label(root, text="Record these audio sources into the clip (Ctrl/Shift = multi):"
              ).pack(anchor="w", padx=10, pady=(8, 0))
    src_lb = tk.Listbox(root, selectmode=tk.EXTENDED, height=6,
                        exportselection=False)
    for n in dshow:
        src_lb.insert(tk.END, n)
    src_lb.pack(fill="x", padx=10)
    # preselect last run's sources, else the auto-detected mic + loopback
    saved_sources = cfg.get("sources")
    if saved_sources:
        for i, n in enumerate(dshow):
            if n in saved_sources:
                src_lb.selection_set(i)
    else:
        auto_mic, auto_loop = pick_devices(args.mic, args.loopback, names=dshow)
        for i, n in enumerate(dshow):
            if n in (auto_mic, auto_loop):
                src_lb.selection_set(i)

    # --- desktop audio via WASAPI loopback (no Stereo Mix / virtual cable) ---
    has_sc = have_soundcard()
    desk_var = tk.BooleanVar(value=bool(cfg.get("desktop_audio", False)) and has_sc)
    desk_chk = ttk.Checkbutton(
        root, variable=desk_var,
        text=("Also record desktop audio - WASAPI loopback, no cable needed" if has_sc
              else "Desktop audio (no cable) - run: pip install soundcard"))
    if not has_sc:
        desk_chk.config(state="disabled")
    desk_chk.pack(anchor="w", padx=10, pady=(4, 0))

    # --- threshold ---
    thr = tk.DoubleVar(value=cfg.get("threshold", args.threshold))
    ttk.Label(root, text="Mic trigger threshold (0-1)").pack(anchor="w", padx=10, pady=(8, 0))
    ttk.Scale(root, from_=0.0, to=1.0, variable=thr,
              command=lambda v: _set_thr()).pack(fill="x", padx=10)
    thr_lbl = ttk.Label(root, text="%.3f" % thr.get())
    thr_lbl.pack(anchor="w", padx=10)

    def _set_thr():
        thr_lbl.config(text="%.3f" % thr.get())
        if clipper["obj"]:
            clipper["obj"].threshold = thr.get()

    # --- level meter (canvas: bar + threshold marker + state text) ---
    ttk.Label(root, text="Live mic level (red = would clip):").pack(anchor="w", padx=10, pady=(8, 0))
    MW, MH = 500, 30
    meter = tk.Canvas(root, width=MW, height=MH, bg="#222", highlightthickness=0)
    meter.pack(padx=10)

    # --- test mode ---
    ttk.Checkbutton(root, text="Test mode (watch levels, never save a clip)",
                    variable=test_var,
                    command=lambda: _set_test()).pack(anchor="w", padx=10, pady=(6, 0))

    def _set_test():
        if clipper["obj"]:
            clipper["obj"].test_mode = test_var.get()
        log("Test mode " + ("ON - no clips will be saved." if test_var.get() else "OFF."))

    # --- buttons ---
    btns = ttk.Frame(root); btns.pack(pady=8)

    def change_folder():
        d = filedialog.askdirectory(initialdir=folder.get(), title="Choose clips folder")
        if d:
            folder.set(d)
            if clipper["obj"]:
                clipper["obj"].clipdir = d
            log("Clips folder: " + d)

    def _selected_sources():
        return [dshow[i] for i in src_lb.curselection()]

    def _encoder():
        return ENCODER_LABELS.get(enc_cb.get(), "libx264")

    def _buffer_mode():
        return BUFFER_LABELS.get(bufmode_cb.get(), "ram")

    def _sync_buf_entry():
        buf_entry.config(state="normal" if _buffer_mode() == "disk" else "disabled")

    def _lock_inputs(locked):
        st = "disabled" if locked else "normal"
        ro = "disabled" if locked else "readonly"
        mic_cb.config(state=ro)
        mon_cb.config(state=ro)
        quality_cb.config(state=ro)
        enc_cb.config(state=ro)
        bufmode_cb.config(state=ro)
        src_lb.config(state=st)
        pre_spin.config(state=st)
        post_spin.config(state=st)
        buf_entry.config(state=st)
        if has_sc:
            desk_chk.config(state=st)      # leave disabled if soundcard is missing
        if not locked:
            _sync_buf_entry()              # folder stays disabled in RAM mode

    def _selected_region():
        i = mon_cb.current()
        if i <= 0 or not monitors:
            return None                    # all monitors
        return monitors[i - 1][:4]         # (x, y, w, h)

    def _scale_h():
        return QUALITY_PRESETS.get(quality_cb.get())

    def _window():
        try:
            return max(1, int(pre_var.get())), max(1, int(post_var.get()))
        except (TypeError, ValueError, tk.TclError):
            return PRE, POST

    def _capture_wh():
        """Resolution ffmpeg will actually encode, for the footprint estimate."""
        region = _selected_region()
        if region:
            cw, ch = region[2], region[3]
        elif monitors:
            x0 = min(m[0] for m in monitors); y0 = min(m[1] for m in monitors)
            x1 = max(m[0] + m[2] for m in monitors); y1 = max(m[1] + m[3] for m in monitors)
            cw, ch = x1 - x0, y1 - y0
        else:
            cw, ch = 1920, 1080
        sh = _scale_h()
        if sh and ch:
            return max(2, round(cw * sh / ch / 2) * 2), sh
        return cw, ch

    def _update_stats():
        if not stats_var.get():
            stats_lbl.config(text="")
            return
        w, h = _capture_wh()
        pre, post = _window()
        retention = pre + post + MAX_EXTEND + 10
        e = estimate_footprint(w, h, FPS, retention, pre + post)
        loc = "RAM" if _buffer_mode() == "ram" else "disk"
        stats_lbl.config(text=(
            "%dx%d @%dfps  ~%.1f Mb/s   buffer ~%d MB on %s (%ds)   clip ~%d MB   "
            "ffmpeg RAM ~%d MB" % (w, h, FPS, e["mbps"], e["buffer_mb"], loc,
                                   retention, e["clip_mb"], e["ram_mb"])))

    def _save_cfg():
        save_settings({"monitor": mon_cb.current(), "mic": mic_cb.get(),
                       "sources": _selected_sources(), "threshold": thr.get(),
                       "folder": folder.get(), "quality": quality_cb.get(),
                       "pre": _window()[0], "post": _window()[1],
                       "bufdir": buf_var.get().strip() or BUFDIR,
                       "buffer_mode": _buffer_mode(), "encoder": _encoder(),
                       "desktop_audio": desk_var.get(), "stats": stats_var.get()})

    def start():
        if clipper["obj"]:
            return
        trig = mics[mic_cb.current()][0] if mics else None
        pre, post = _window()
        c = Clipper(thr.get(), _selected_sources(), trigger_mic=trig,
                    on_event=log, clipdir=folder.get(), test_mode=test_var.get(),
                    region=_selected_region(), pre=pre, post=post,
                    scale_h=_scale_h(), bufdir=buf_var.get().strip() or BUFDIR,
                    buffer_mode=_buffer_mode(), encoder=_encoder(),
                    desktop_audio=desk_var.get())
        if c.start():
            clipper["obj"] = c
            _save_cfg()
            start_btn.config(state="disabled"); stop_btn.config(state="normal")
            _lock_inputs(True)

    def stop():
        c = clipper["obj"]
        if not c:
            return
        clipper["obj"] = None
        stop_btn.config(state="disabled")
        # ponytail: teardown waits up to 5s for ffmpeg to finalize; do it off the
        # UI thread so the window doesn't freeze, then re-enable controls.
        def worker():
            c.stop()
            root.after(0, lambda: (start_btn.config(state="normal"), _lock_inputs(False)))
        threading.Thread(target=worker, daemon=True).start()

    def open_folder():
        os.makedirs(folder.get(), exist_ok=True)
        os.startfile(folder.get())

    start_btn = ttk.Button(btns, text="Start", command=start); start_btn.grid(row=0, column=0, padx=4)
    stop_btn = ttk.Button(btns, text="Stop", command=stop, state="disabled"); stop_btn.grid(row=0, column=1, padx=4)
    ttk.Button(btns, text="Open clips folder", command=open_folder).grid(row=0, column=2, padx=4)

    fr = ttk.Frame(root); fr.pack(fill="x", padx=10, pady=(2, 4))
    ttk.Label(fr, text="Clips →").pack(side="left")
    ttk.Label(fr, textvariable=folder, foreground="gray").pack(side="left", padx=(4, 4))
    ttk.Button(fr, text="Change…", command=change_folder).pack(side="right")

    listbox = tk.Listbox(root, height=6); listbox.pack(fill="both", expand=True, padx=10, pady=(4, 4))
    ttk.Label(root, textvariable=status, foreground="gray").pack(anchor="w", padx=10, pady=(0, 6))

    def draw_meter(level, threshold, label, color):
        meter.delete("all")
        meter.create_rectangle(0, 0, int(level * MW), MH, fill=color, outline="")
        x = int(threshold * MW)
        meter.create_line(x, 0, x, MH, fill="white", width=2)
        meter.create_text(8, MH // 2, anchor="w", text=label, fill="white",
                          font=("", 10, "bold"))

    def tick():
        c = clipper["obj"]
        if c:
            lvl = c.level
            if c.state == "error":
                label, color = "RECORDER DIED - see buffer/ffmpeg.log", "#c0392b"
            elif c.test_mode:
                label, color = "TEST - no clips", "#3498db"
            elif c.state == "capturing":
                label, color = "CAPTURING...", "#e67e22"
            elif c.state == "cooldown":
                label, color = "cooldown", "#7f8c8d"
            elif lvl >= thr.get():
                label, color = "WOULD CLIP", "#e74c3c"
            else:
                label, color = "armed", "#2ecc71"
            draw_meter(lvl, thr.get(), label, color)
            root.title("Loudred - %d clip(s) saved" % c.clip_count)
        else:
            draw_meter(0, thr.get(), "stopped", "#444")
            root.title("Loudred - instant replay clipper")
        root.after(100, tick)
    tick()
    _update_stats()                 # show the footprint now if the toggle is on

    def on_close():
        try:
            _save_cfg()
        except Exception:
            pass
        c = clipper["obj"]
        if c:
            clipper["obj"] = None
            c.stop()                       # synchronous: we're quitting, finalize cleanly
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main():
    set_dpi_aware()   # must run before enumerating monitors / building the GUI
    p = argparse.ArgumentParser(description="Peak-triggered screen+audio clipper")
    p.add_argument("--threshold", type=float, default=0.1, help="mic RMS loudness 0..1 to trigger a clip")
    p.add_argument("--mic", help="override DirectShow mic device name")
    p.add_argument("--loopback", help="override DirectShow loopback device name")
    p.add_argument("--list-devices", action="store_true", help="list dshow audio devices and exit")
    args = p.parse_args()
    if not shutil.which("ffmpeg"):
        msg = ("ffmpeg was not found on your PATH. Install it so that "
               "`ffmpeg -version` works in a terminal, then retry (see README).")
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            messagebox.showerror("Loudred - ffmpeg missing", msg)
            r.destroy()
        except Exception:
            print(msg)
        return
    if args.list_devices:
        for n in list_dshow_audio():
            print(n)
        return
    run_gui(args)


if __name__ == "__main__":
    main()
