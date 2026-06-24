# Loudred — planned improvements

Backlog of things to add. Roughly ordered by value-for-effort. References point
at the current code in `app.py`.

> **Status:** #1, #2, #3 are **DONE** (kept below for context). #4 and the
> smaller fixes remain.

## 1. Test mode (mic test that doesn't clip)  ✅ DONE
Let the user check their mic/levels without triggering a save.

- Add a **"Test mode"** checkbox in the GUI. While on, `_on_audio` still
  updates `self.level` (so the meter and the threshold indicator work) but
  **never starts a clip** — gate the trigger in [app.py:_on_audio](app.py#L209).
- Cheapest version: a `self.test_mode` flag on `Clipper`; `if self.test_mode:
  return` before the arm/trigger block.

## 2. Make "will this noise clip?" obvious  ✅ DONE
The current meter is a bare progress bar — you can't tell where the threshold
sits relative to the live level.

- Draw the **threshold as a marker line on the meter**, or color the meter
  red when `level >= threshold`, green/grey otherwise (update in the `tick()`
  loop, [app.py:tick](app.py#L290)).
- Show a big **"WOULD CLIP" / "armed"** status label that flips live.
- Add a **"recent peaks" readout** (last N peak values + whether each crossed)
  so tuning is concrete.
- Optional: a short **"clip in 3…2…1"** countdown / "capturing post-roll"
  state so it's clear a clip is in progress vs idle.

## 3. Device + channel selection in the GUI  ✅ DONE
Currently mic/loopback are auto-picked or CLI-only ([app.py:pick_devices](app.py#L100)).

- **Mic dropdown**: populate from `list_dshow_audio()` ([app.py:84](app.py#L84));
  changing it restarts the sounddevice trigger stream and the ffmpeg recorder.
  Note: the **trigger** stream uses `sounddevice` (its own device list) while
  ffmpeg uses the **dshow** name — these need to map to the same physical mic
  (add a `--mic-index` for sounddevice, surface both in the dropdown).
- **Loopback dropdown**: same list, lets the user pick the right Voicemeeter
  bus / Stereo Mix / CABLE instead of relying on the hint guess
  ([app.py:LOOPBACK_HINTS](app.py#L26)).
- **Arbitrary number of audio sources**: today the recorder hardcodes 1 mic +
  optional 1 loopback ([app.py:build_ffmpeg_cmd](app.py#L107)). Generalize to a
  list of selected dshow audio devices, add one `-f dshow -i audio=...` per
  device, and feed all of them into `amix=inputs=N`. The GUI becomes a
  multi-select checklist of audio devices.

## 4. (Bonus) Per-application audio capture
Capture only a specific app's audio (e.g. just Discord, not the whole desktop).

- Windows has **per-app audio loopback via WASAPI process loopback**
  (`AUDCLNT_STREAMFLAGS` process-loopback, Win10 2004+). ffmpeg/dshow can't do
  this directly — options:
  - Use a Python WASAPI lib that supports process loopback
    (e.g. `pyaudiowpatch`, or `soundcard`) to capture a chosen process, then
    feed that PCM into ffmpeg via a pipe (`-f s16le -i pipe:`).
  - Or rely on the user routing one app to a dedicated Voicemeeter/virtual
    bus and selecting that bus in #3 — zero new code, just documentation.
- This is the most involved item; the Voicemeeter-routing workaround gives
  ~80% of the value for ~0 effort and may be enough.

## Pain points found in code scan (bugs, not features)

Ordered by how much they hurt. **All seven are now fixed.**

1. **ffmpeg-missing crashes at startup.**  ✅ DONE — [main](app.py) checks
   `shutil.which("ffmpeg")` and shows a friendly messagebox (or prints, if Tk is
   unavailable) instead of a traceback; [list_dshow_audio](app.py#L62) also
   swallows `FileNotFoundError`.

2. **ffmpeg failures are invisible.**  ✅ DONE — recorder stderr now goes to
   `buffer/ffmpeg.log` and a `_watch_proc` thread reports "Recorder stopped
   unexpectedly (exit N)" and flips state to `error` (red meter) when ffmpeg
   dies on its own.

3. **Trigger fires on single-sample clicks.**  ✅ DONE — `peak_level` replaced by
   [rms_level](app.py) (RMS of the block). Default `--threshold` dropped to 0.1
   to match the new scale. A single 0.9 click now reads <0.05.

4. **Back-to-back moments are dropped.**  ✅ DONE — a peak during capture now
   pushes the clip's end out (via `_last_peak`, capped at `MAX_EXTEND=20s`) so a
   burst of loud moments becomes one continuous clip instead of being lost in the
   post-roll window. Window selection generalized to
   [select_range](app.py) (`first_peak-PRE .. last_peak+POST`); the cap keeps the
   longest clip (`PRE+MAX_EXTEND+POST = 50s`) inside the 60s buffer.

5. **stop() freezes the UI.**  ✅ DONE — the Stop button runs teardown on a
   worker thread and re-enables controls via `root.after`; window-close stays
   synchronous (we're quitting anyway).

6. **No settings persistence.**  ✅ DONE — monitor/mic/sources/threshold/folder
   save to `loudred_settings.json` (next to `app.py`) on start and on close, and
   reload on launch.

7. **Stale `concat_*.txt` on crash.**  ✅ DONE — [prune_loop](app.py) now also
   sweeps `concat_*.txt` older than `RETENTION`.

## Smaller fixes worth doing alongside
- **Trigger/segment clock alignment**: trigger time is `time.time()`; segment
  names are wall-clock — verify the peak lands ~15s in, adjust offset if not
  ([app.py:_do_clip](app.py#L219), [app.py:select_segments](app.py#L56)).
- **Sub-second trim**: clips are 1s-granular; add precise `-ss/-t` if needed.
- **Mic opened twice**: ffmpeg records the mic and sounddevice triggers off it.
  If a device ever rejects the double-open, switch the trigger to parse ffmpeg
  `silencedetect`/`ametadata` ([app.py:start](app.py#L181) ponytail note).
- **Monitor selection**: ✅ DONE — GUI has a monitor picker (defaults to the
  primary monitor) that crops gdigrab via `-offset_x/-offset_y/-video_size`.

## Roadmap — improvements from later scans (not bugs)

Ranked by value-for-effort. None of these are defects; they're enhancements
found while reviewing the working code.

1. **"Clip now" manual trigger.** The most common miss for an instant-replay
   tool is a great moment you *didn't* shout at. A GUI button (or hotkey) that
   fires `_do_clip` on demand captures the last ~15s regardless of loudness.
   Cheap: reuses existing machinery, ~5 lines in [run_gui](app.py). A global
   hotkey (Windows `RegisterHotKey` via ctypes) is the bigger version; the
   button is the lazy 80%.
2. **Auto-recover from "RECORDER DIED".** When ffmpeg dies, [tick](app.py) shows
   red but Start stays disabled and the trigger mic stays open — stuck until a
   manual Stop→Start. Fix: in `tick`, if `state == "error"`, call the GUI
   `stop()` once to tear down the dead clipper and re-enable Start. ~3 lines.
3. **Clamp the meter bar.** [draw_meter](app.py) does `int(level * MW)`; a driver
   returning level > 1.0 overflows the canvas. `min(level, 1.0)`.
4. **argparse description** still says "Peak-triggered" — it's RMS now (cosmetic).
5. **Threshold auto-calibration** (bigger): measure ambient RMS for a second on
   start and set the default threshold above it, so a new user isn't guessing.
   Test mode already covers manual tuning, so this is a nice-to-have.

Deliberately NOT planned (over-engineering for this tool): refactoring the
~180-line `run_gui` (works, no bug), pinning `requirements.txt` (personal tool;
wrong pins are worse than none), clips-folder retention (clips are the output —
accumulating is correct).

## Shipped — capture settings + stats + RAM buffer

Added as GUI settings (all persisted to `loudred_settings.json`):

- **Capture resolution** — Native / 1080p / 720p / 480p, via `scale_h` in
  [build_ffmpeg_cmd](app.py) (`scale=-2:H` folded into the filtergraph alongside
  any `amix`).
- **Clip window** — seconds before/after are now `Clipper(pre=, post=)`; buffer
  retention derives from them (`pre + post + MAX_EXTEND + 10`). `build_clip` /
  `prune_loop` take the window/retention as params.
- **Stats for nerds** — [estimate_footprint](app.py) gives bitrate, rolling-buffer
  MB, per-clip MB and rough ffmpeg RAM from resolution × fps × `BITS_PER_PIXEL`;
  a GUI toggle shows it live as you change settings.
- **Buffer folder** — `Clipper(bufdir=)` makes the rolling-buffer location
  configurable, so it can point at a RAM disk to avoid SSD write wear. README has
  the ImDisk/OSFMount setup.

### ✅ DONE: zero-dependency in-RAM ring buffer (the ShadowPlay/ReLive approach)
RAM is now the **default** rolling buffer — no RAM disk, no SSD writes until a
clip is saved. ffmpeg streams fragmented MP4 to `pipe:1`
(`-movflags frag_keyframe+empty_moov+default_base_moof -g fps -f mp4`);
[iter_mp4_boxes](app.py) splits the stream on MP4 box boundaries and
[RamBuffer](app.py) keeps the last `retention` seconds of keyframe-aligned
`moof`+`mdat` fragments in a `deque`, indexed by arrival time. On a peak it writes
`init + fragments` straight to an mp4 — no remux. Disk-segment mode is kept as an
option (`buffer_mode="disk"`) for very long buffers / low-RAM machines.

**Caveat still standing:** the box-parsing + ring-buffer logic is unit-tested, but
playback parity of the assembled fMP4 (start-PTS, A/V sync across players) can only
be confirmed on a real run with real ffmpeg — verify a saved clip plays in your
player of choice.

### ✅ DONE: GPU hardware encoder option
Video-encoder dropdown — CPU (`libx264`) or GPU (`h264_amf` / `h264_nvenc` /
`h264_qsv`), via [_venc_args](app.py). Offloads encoding from the CPU like ReLive;
does not change disk/RAM use (bitrate is similar). If the chosen encoder isn't in
the user's ffmpeg build, ffmpeg exits and the "RECORDER DIED" state surfaces it.
