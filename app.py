"""Loudred - peak-triggered screen+audio clipper (instant replay).

Continuously buffers screen video + desktop/Discord audio (loopback) + mic into
1s segments. When the mic crosses a loudness threshold, saves the 30s window
(15s before + 15s after) as one mp4.

Needs ffmpeg on PATH. See README.md.
"""
import argparse
import ctypes
import glob
import os
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import datetime

import numpy as np
import sounddevice as sd

SEG_LEN = 1                  # seconds per segment
PRE = 15                     # seconds before the peak
POST = 15                    # seconds after the peak
RETENTION = 60              # keep this many seconds of buffer on disk
FPS = 30
BUFDIR = "buffer"
CLIPDIR = "clips"
SEG_GLOB = "seg_*.ts"
SEG_FMT = "seg_%Y%m%d_%H%M%S.ts"
LOOPBACK_HINTS = ("stereo mix", "cable output", "loopback", "what u hear",
                  "what you hear", "voicemeeter out b1")


# ---- pure logic (covered by test_app.py) ----

def peak_level(block):
    """Max absolute sample of a float32 block, 0..1."""
    if len(block) == 0:
        return 0.0
    return float(np.abs(block).max())


def parse_seg_time(name):
    """Epoch seconds from a seg_YYYYmmdd_HHMMSS.ts filename (local time)."""
    base = os.path.basename(name)
    return datetime.strptime(base, SEG_FMT).timestamp()


def select_segments(segs, trigger, pre=PRE, post=POST, seg_len=SEG_LEN):
    """Pick segments overlapping [trigger-pre, trigger+post].

    segs: list of (name, start_epoch). Returns names sorted by start time.
    """
    lo, hi = trigger - pre, trigger + post
    chosen = [(s, n) for n, s in segs if s + seg_len >= lo and s <= hi]
    return [n for _, n in sorted(chosen)]


# ---- device discovery ----

def list_dshow_audio():
    """Names of DirectShow audio devices via ffmpeg (parsed from stderr)."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True,
    ).stderr
    # ffmpeg tags each device line "(audio)"/"(video)"; skip "Alternative name" lines.
    names = []
    for line in out.splitlines():
        if line.rstrip().endswith("(audio)") and '"' in line and "Alternative name" not in line:
            names.append(line.split('"')[1])
    return names


def pick_devices(mic_override=None, loop_override=None):
    """Return (mic_name, loopback_name_or_None)."""
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

def build_ffmpeg_cmd(audio_devices, region=None):
    """ffmpeg cmd: screen video + any number of dshow audio sources mixed to one track.

    region = (x, y, w, h) crops gdigrab to one monitor; None = whole virtual desktop.
    """
    cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(FPS)]
    if region:
        x, y, w, h = region
        cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", "%dx%d" % (w, h)]
    cmd += ["-i", "desktop"]
    for d in audio_devices:
        cmd += ["-f", "dshow", "-i", "audio=" + d]
    n = len(audio_devices)
    if n == 0:
        cmd += ["-map", "0:v"]
    elif n == 1:
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        ins = "".join("[%d:a]" % (i + 1) for i in range(n))
        cmd += ["-filter_complex", "%samix=inputs=%d:duration=longest[a]" % (ins, n),
                "-map", "0:v", "-map", "[a]"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-f", "segment", "-segment_time", str(SEG_LEN),
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(BUFDIR, SEG_FMT)]
    return cmd


def prune_loop(stop):
    """Delete buffer segments older than RETENTION seconds."""
    while not stop.is_set():
        cutoff = time.time() - RETENTION
        for f in glob.glob(os.path.join(BUFDIR, SEG_GLOB)):
            try:
                if parse_seg_time(f) < cutoff:
                    os.remove(f)
            except (ValueError, OSError):
                pass
        stop.wait(5)


def build_clip(trigger, log, clipdir=CLIPDIR):
    """Concat the segments around `trigger` into one mp4. Lossless copy."""
    segs = []
    for f in glob.glob(os.path.join(BUFDIR, SEG_GLOB)):
        try:
            segs.append((f, parse_seg_time(f)))
        except ValueError:
            pass
    chosen = select_segments([(f, t) for f, t in segs], trigger)
    if not chosen:
        log("No segments to clip (buffer empty?)")
        return None
    listfile = os.path.join(BUFDIR, "concat_%d.txt" % int(trigger))
    with open(listfile, "w") as fh:
        for n in chosen:
            fh.write("file '%s'\n" % os.path.abspath(n).replace("\\", "/"))
    os.makedirs(clipdir, exist_ok=True)
    out = os.path.join(clipdir,
                       "clip_%s.mp4" % datetime.fromtimestamp(trigger).strftime("%Y%m%d_%H%M%S"))
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0",
         "-i", listfile, "-c", "copy", out],
        capture_output=True, text=True)
    try:
        os.remove(listfile)
    except OSError:
        pass
    if r.returncode != 0:
        log("Clip failed: " + r.stderr.strip().splitlines()[-1:] [0] if r.stderr else "ffmpeg error")
        return None
    log("Saved " + out)
    return out


class Clipper:
    """Owns the ffmpeg recorder, pruner, mic-trigger and clip building."""

    def __init__(self, threshold, audio_devices, trigger_mic=None, on_event=print,
                 clipdir=CLIPDIR, test_mode=False, region=None):
        self.threshold = threshold
        self.audio_devices = list(audio_devices)   # dshow names recorded into the clip
        self.trigger_mic = trigger_mic             # sounddevice index/name; None = default
        self.region = region                       # (x,y,w,h) monitor crop; None = all
        self.on_event = on_event
        self.clipdir = clipdir
        self.test_mode = test_mode
        self.level = 0.0           # latest mic peak, for the GUI meter
        self.clip_count = 0
        self.state = "idle"        # idle | armed | capturing | cooldown
        self._proc = None
        self._stop = threading.Event()
        self._stream = None
        self._armed = True
        self._lock = threading.Lock()

    def start(self):
        os.makedirs(BUFDIR, exist_ok=True)
        os.makedirs(self.clipdir, exist_ok=True)
        if not self.audio_devices:
            self.on_event("WARNING: no audio sources selected - clips will be video-only.")
        vid = ("monitor %dx%d at (%d,%d)" % (self.region[2], self.region[3],
               self.region[0], self.region[1])) if self.region else "ALL monitors (whole desktop)"
        self.on_event("Video: " + vid)
        self.on_event("Recording sources: " + (", ".join(self.audio_devices) or "NONE"))
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
        self._proc = subprocess.Popen(build_ffmpeg_cmd(self.audio_devices, self.region),
                                      stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=prune_loop, args=(self._stop,), daemon=True).start()
        self.state = "armed"
        self.on_event("Recording. Buffering %ds; clips = %ds before + %ds after a peak.%s"
                      % (RETENTION, PRE, POST, "  [TEST MODE - no clips]" if self.test_mode else ""))
        return True

    def _on_audio(self, indata, frames, t, status):
        lvl = peak_level(indata[:, 0])
        self.level = lvl
        if self.test_mode:
            return
        if lvl >= self.threshold:
            with self._lock:
                if self._armed:
                    self._armed = False
                    self.state = "capturing"
                    trig = time.time()
                    threading.Thread(target=self._do_clip, args=(trig,), daemon=True).start()

    def _do_clip(self, trig):
        self.on_event("Peak! Capturing window...")
        time.sleep(POST + 1)            # let post-roll land on disk
        out = build_clip(trig, self.on_event, self.clipdir)
        if out:
            self.clip_count += 1
        self.state = "cooldown"
        time.sleep(1)                   # brief cooldown
        with self._lock:
            self._armed = True
            self.state = "armed"

    def stop(self):
        self._stop.set()
        if self._stream:
            self._stream.stop(); self._stream.close(); self._stream = None
        if self._proc:
            try:
                self._proc.stdin.write(b"q")   # ask ffmpeg to finalize segments
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.terminate()
            self._proc = None
        self.state = "idle"
        self.level = 0.0
        self.on_event("Stopped.")


# ---- GUI ----

def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog

    root = tk.Tk()
    root.title("Loudred - instant replay clipper")
    root.geometry("520x680")

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

    status = tk.StringVar(value="Idle. Pick devices, set threshold, then Start.")
    folder = tk.StringVar(value=os.path.abspath(CLIPDIR))
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
    mon_cb.current(default_mon if mon_opts else 0)
    mon_cb.pack(fill="x", padx=10)

    # --- trigger mic (sounddevice) ---
    mics = list_input_mics()
    ttk.Label(root, text="Trigger mic (its loudness arms a clip):").pack(anchor="w", padx=10, pady=(10, 0))
    mic_cb = ttk.Combobox(root, state="readonly",
                          values=[name for _, name in mics] or ["(no input devices)"])
    mic_cb.current(0)
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
    # preselect the auto-detected mic + loopback
    auto_mic, auto_loop = pick_devices(args.mic, args.loopback)
    for i, n in enumerate(dshow):
        if n in (auto_mic, auto_loop):
            src_lb.selection_set(i)

    # --- threshold ---
    thr = tk.DoubleVar(value=args.threshold)
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

    def _lock_inputs(locked):
        st = "disabled" if locked else "normal"
        ro = "disabled" if locked else "readonly"
        mic_cb.config(state=ro)
        mon_cb.config(state=ro)
        src_lb.config(state=st)

    def _selected_region():
        i = mon_cb.current()
        if i <= 0 or not monitors:
            return None                    # all monitors
        return monitors[i - 1][:4]         # (x, y, w, h)

    def start():
        if clipper["obj"]:
            return
        trig = mics[mic_cb.current()][0] if mics else None
        c = Clipper(thr.get(), _selected_sources(), trigger_mic=trig,
                    on_event=log, clipdir=folder.get(), test_mode=test_var.get(),
                    region=_selected_region())
        if c.start():
            clipper["obj"] = c
            start_btn.config(state="disabled"); stop_btn.config(state="normal")
            _lock_inputs(True)

    def stop():
        if clipper["obj"]:
            clipper["obj"].stop(); clipper["obj"] = None
        start_btn.config(state="normal"); stop_btn.config(state="disabled")
        _lock_inputs(False)

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
            if c.test_mode:
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

    root.protocol("WM_DELETE_WINDOW", lambda: (stop(), root.destroy()))
    root.mainloop()


def main():
    set_dpi_aware()   # must run before enumerating monitors / building the GUI
    p = argparse.ArgumentParser(description="Peak-triggered screen+audio clipper")
    p.add_argument("--threshold", type=float, default=0.3, help="mic peak 0..1 to trigger a clip")
    p.add_argument("--mic", help="override DirectShow mic device name")
    p.add_argument("--loopback", help="override DirectShow loopback device name")
    p.add_argument("--list-devices", action="store_true", help="list dshow audio devices and exit")
    args = p.parse_args()
    if args.list_devices:
        for n in list_dshow_audio():
            print(n)
        return
    run_gui(args)


if __name__ == "__main__":
    main()
