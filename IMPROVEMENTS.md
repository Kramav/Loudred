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
